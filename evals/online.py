"""Online evaluation: score a sample of *real* agent traces after the fact, with no reference answers.

    mise run online-eval                         # last 24h, 50% sample, up to 50 traces
    mise run online-eval -- --hours 2 --sample 1.0

Run it on a schedule (cron, an Airflow DAG, a Kubernetes CronJob). For each sampled trace it:
  1. reads the question, the answer and every tool result back from Langfuse's public API,
  2. runs the reference-free checks: grounded instance IDs (code) and the LLM judge without a reference,
  3. writes scores named online_* onto the trace, where dashboards and alerts can use them.

Low scores are your queue of candidate eval cases: read the trace, decide the right answer, and add it to
the golden set. That is the loop from lesson 01.

Sampling is deterministic on the trace id, so re-running never double-scores a different subset, and
traces that already carry an online score are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import yaml

from evals.cases import Case
from evals.graders import deterministic
from evals.graders.judge import JudgeError, judge
from evals.run import post_score
from evals.trajectory import ToolCall, Trajectory
from observ.secrets import load_eval_runner_secrets

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:33000")
MARKER_SCORE = "online_judge_groundedness"


def _text_from_parts(raw: str | None) -> str:
    """Our spans store messages in the GenAI conventions' parts format; pull the text back out."""
    if not raw:
        return ""
    try:
        msgs = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(msgs, list):
        return "\n".join(p.get("content", "") for m in msgs if isinstance(m, dict)
                         for p in m.get("parts", []) if p.get("type") == "text")
    return str(msgs)


def _field(o, name: str):
    """API objects are pydantic models with snake_case attributes and camelCase JSON aliases."""
    if isinstance(o, dict):
        return o.get(name)
    return getattr(o, re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower(), None)


def sampled(trace_id: str, rate: float) -> bool:
    return int(trace_id[:8], 16) / 0xFFFFFFFF < rate


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--sample", type=float, default=0.5)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--environment", nargs="+", default=["development", "production"])
    args = p.parse_args()

    cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text())
    load_eval_runner_secrets()
    from langfuse import Langfuse
    lf = Langfuse(base_url=LANGFUSE_HOST, tracing_enabled=False)
    client = anthropic.AsyncAnthropic(max_retries=4)

    roots = lf.api.observations.get_many(
        type="AGENT", is_root_observation=True, environment=args.environment, fields="core,basic,io",
        from_start_time=datetime.now(UTC) - timedelta(hours=args.hours), limit=min(1000, args.limit * 4)).data
    todo = [r for r in roots if sampled(_field(r, "traceId"), args.sample)][: args.limit]
    print(f"{len(roots)} agent traces in the last {args.hours}h, {len(todo)} sampled")

    scored = skipped = 0
    for root in todo:
        trace_id, root_id = _field(root, "traceId"), _field(root, "id")
        if lf.api.scores_v3.get_many_v3(trace_id=trace_id, name=MARKER_SCORE, limit=1).data:
            skipped += 1
            continue
        tools = lf.api.observations.get_many(trace_id=trace_id, type="TOOL", fields="core,basic,io", limit=100).data
        traj = Trajectory(tool_calls=[ToolCall(_field(t, "name"), {}, _field(t, "output") or "", False, 0)
                                      for t in tools])
        question, answer = _text_from_parts(_field(root, "input")), _text_from_parts(_field(root, "output"))
        if not answer:
            continue   # run failed before answering: that shows up in error-rate metrics, not here
        grades = []
        g = deterministic.grounded_ids(Case(id="online", category="online", question=question, reference=""),
                                       answer, traj)
        if g:
            grades.append(g)
        try:
            jr = await judge(client, model=cfg["judge"]["model"], effort=cfg["judge"]["effort"], question=question,
                             answer=answer, tool_results=traj.tool_results_text(), reference=None)
            grades += jr.grades
        except (JudgeError, anthropic.APIError) as e:
            print(f"  judge failed on {trace_id}: {e}")
            continue
        for g in grades:
            post_score(lf, trace_id=trace_id, observation_id=root_id, name=f"online_{g.name}", passed=g.passed,
                       comment=g.detail)
        scored += 1
        bad = [g.name for g in grades if not g.passed]
        print(f"  {'ok  ' if not bad else 'FLAG'} {trace_id} {question[:60]!r}{'  -> ' + ', '.join(bad) if bad else ''}")
    print(f"scored {scored}, already scored {skipped}")


if __name__ == "__main__":
    asyncio.run(main())
