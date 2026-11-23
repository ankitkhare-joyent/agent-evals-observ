"""Offline eval runner: golden cases x reps x agent variants, graded by code and by an LLM judge.

    mise run eval                                   # uses evals/config.yaml
    mise run eval -- --reps 1 --only lookup         # quick iteration on one category
    mise run eval -- --variants raw_loop langgraph  # compare implementations on identical cases

Outputs (reports/<run_id>/): cases.json, results.jsonl (one row per scored run, with full trajectory),
errors.jsonl (runs that never produced a scorable answer), summary.md, junit.xml. Exit code 1 if a gate
fails, so the same command is your CI check.

In Langfuse every run is one trace, linked to the dataset item and to this experiment run through
OpenTelemetry attributes (langfuse.experiment.*), with every grade attached as a score.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

import anthropic
import yaml
from opentelemetry import baggage, context, trace
from opentelemetry.trace import Status, StatusCode

from evals import report
from evals.cases import Case, build_cases
from evals.graders import deterministic
from evals.graders.judge import JudgeError, judge
from evals.trajectory import SpanRecorder, build_trajectory
from observ import genai
from observ.agent_under_test import VARIANTS, agent_model, run_agent
from observ.secrets import load_eval_runner_secrets
from observ.telemetry import setup_telemetry

ROOT = Path(__file__).resolve().parent.parent
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:33000")
DATASET = "ec2-agent-golden"
tracer = trace.get_tracer("agent-evals-observ.evals")


SNAPSHOT_RE = re.compile(r'"(?:latest_snapshot_ts|snapshot_ts)": "([0-9T:\-Z+]+)"')


def latest_snapshot_seen(traj) -> str | None:
    """Newest inventory snapshot any tool result mentioned (ISO strings compare correctly as text)."""
    return max(SNAPSHOT_RE.findall(traj.tool_results_text()), default=None)


def langfuse_client():
    """Langfuse SDK as an API client only (datasets + scores). Traces go through OTel and the Collector."""
    from langfuse import Langfuse
    return Langfuse(base_url=LANGFUSE_HOST, tracing_enabled=False)


def sync_dataset(lf, cases: list[Case]) -> str | None:
    """Upsert the generated cases as a Langfuse dataset so runs can be compared side by side in the UI."""
    try:
        try:
            ds = lf.get_dataset(DATASET)
        except Exception:  # noqa: BLE001 - not found
            lf.create_dataset(name=DATASET, description="Generated golden cases for the EC2 inventory agent")
            ds = lf.get_dataset(DATASET)
        for c in cases:
            lf.create_dataset_item(dataset_name=DATASET, id=c.id, input={"question": c.question},
                                   expected_output={"reference": c.reference, "must_include": c.must_include},
                                   metadata={"category": c.category, "snapshot_ts": c.snapshot_ts})
        return ds.id
    except Exception as e:  # noqa: BLE001 - Langfuse being down must not stop the eval
        print(f"warning: could not sync dataset to Langfuse ({e}); continuing without it")
        return None


class Runner:
    def __init__(self, cfg: dict, run_id: str, out: Path, recorder: SpanRecorder, lf, dataset_id: str | None):
        self.cfg, self.run_id, self.out, self.recorder, self.lf = cfg, run_id, out, recorder, lf
        self.sem = asyncio.Semaphore(cfg["max_concurrency"])
        self.judge_client = anthropic.AsyncAnthropic(max_retries=4)
        self.model = agent_model()
        exp = {"langfuse.experiment.id": run_id, "langfuse.experiment.name": f"golden-{run_id}",
               "langfuse.environment": "eval"}
        if dataset_id:
            exp["langfuse.experiment.dataset.id"] = dataset_id
        self.exp_baggage = exp
        self.rows: list[dict] = []
        self.errors: list[dict] = []

    def _root_context(self):
        ctx = context.Context()          # empty: each item is its own trace, never a child of something else
        for k, v in self.exp_baggage.items():
            ctx = baggage.set_baggage(k, v, ctx)
        return ctx

    async def run_one(self, variant: str, case: Case, rep: int) -> None:
        async with self.sem:
            root = tracer.start_span(f"eval {case.category}", context=self._root_context(), attributes={
                "langfuse.experiment.item.id": case.id,
                "langfuse.experiment.description": f"variant={variant}",
                "langfuse.experiment.item.expected_output": json.dumps({"reference": case.reference}),
                "langfuse.experiment.item.metadata.category": case.category,
                "langfuse.observation.input": json.dumps({"question": case.question}),
                "eval.variant": variant, "eval.rep": rep,
            })
            sc = root.get_span_context()
            root_id, trace_id = format(sc.span_id, "016x"), format(sc.trace_id, "032x")
            root.set_attribute("langfuse.experiment.item.root_observation_id", root_id)
            ctx = trace.set_span_in_context(root, self._root_context())
            ctx = baggage.set_baggage("langfuse.experiment.item.id", case.id, ctx)
            ctx = baggage.set_baggage("langfuse.experiment.item.root_observation_id", root_id, ctx)
            token = context.attach(ctx)
            base = {"run_id": self.run_id, "variant": variant, "case_id": case.id, "category": case.category,
                    "rep": rep, "question": case.question, "trace_id": trace_id,
                    "trace_url": f"{LANGFUSE_HOST}/project/ec2-agent/traces/{trace_id}"}
            try:
                t0 = time.perf_counter()
                try:
                    with genai.agent_span(agent_name="ec2-inventory-agent", question=case.question, model=self.model,
                                          conversation_id=f"{self.run_id}-{case.id}-{variant}-{rep}",
                                          user_id="eval-runner", extra={"agent.variant": variant}) as span:
                        run = await asyncio.wait_for(run_agent(variant, case.question), self.cfg["case_timeout_s"])
                        genai.set_agent_output(span, run.answer)
                except Exception as e:  # noqa: BLE001 - never produced an answer: infra, not a model grade
                    root.set_status(Status(StatusCode.ERROR, f"infra: {type(e).__name__}"))
                    self.errors.append({**base, "error": f"{type(e).__name__}: {e}",
                                        "traceback": traceback.format_exc()[-2000:],
                                        "wall_s": round(time.perf_counter() - t0, 2)})
                    print(f"  ERROR  {variant:9} {case.id} rep{rep}: {type(e).__name__}: {str(e)[:120]}")
                    return
                traj = build_trajectory(self.recorder.pop(sc.trace_id))
                seen = latest_snapshot_seen(traj)
                if case.snapshot_ts and seen and seen > case.snapshot_ts:
                    # The data moved on after the oracle computed the answer: grading against it would
                    # punish a correct agent. Not a model failure - record it and don't score.
                    root.set_status(Status(StatusCode.ERROR, "stale ground truth"))
                    self.errors.append({**base, "error": f"StaleGroundTruth: case built on {case.snapshot_ts}, "
                                        f"agent saw {seen}", "answer": run.answer})
                    print(f"  STALE  {variant:9} {case.id} rep{rep}: data changed ({case.snapshot_ts} -> {seen})")
                    return
                grades = deterministic.grade_all(case, run.answer, traj)
                judge_cost = 0.0
                if self.cfg["judge"]["enabled"]:
                    with tracer.start_as_current_span("evaluate llm-judge",
                                                      attributes={"langfuse.observation.type": "evaluator"}):
                        try:
                            jr = await judge(self.judge_client, model=self.cfg["judge"]["model"],
                                             effort=self.cfg["judge"]["effort"], question=case.question,
                                             answer=run.answer, tool_results=traj.tool_results_text(),
                                             reference=case.reference)
                            grades += jr.grades
                            judge_cost = genai.cost_usd(self.cfg["judge"]["model"], jr.input_tokens, jr.output_tokens)
                        except (JudgeError, anthropic.APIError) as e:
                            # The grader failed, not the agent: record it and don't score this run at all.
                            root.set_status(Status(StatusCode.ERROR, "judge failed"))
                            self.errors.append({**base, "error": f"JudgeError: {type(e).__name__}: {str(e)[:300]}",
                                                "answer": run.answer})
                            print(f"  JUDGE  {variant:9} {case.id} rep{rep}: {type(e).__name__}: {str(e)[:100]}")
                            return
                passed = all(g.passed for g in grades if g.name not in ("efficiency", "judge_instruction_following"))
                root.set_attribute("langfuse.observation.output", json.dumps({"answer": run.answer}))
                row = {**base, "answer": run.answer, "passed": passed, "grades": [g.__dict__ for g in grades],
                       "trajectory": traj.to_dict(), "judge_cost_usd": round(judge_cost, 6),
                       "agent_stdout": run.stdout}
                self.rows.append(row)
                self._scores(trace_id, root_id, grades, passed)
                bad = [g.name for g in grades if not g.passed]
                print(f"  {'PASS' if passed else 'FAIL'}   {variant:9} {case.id} rep{rep} "
                      f"{traj.latency_s:5.1f}s ${traj.cost_usd:.4f} {len(traj.tool_calls)} tools"
                      f"{'  failed: ' + ', '.join(bad) if bad else ''}")
            finally:
                root.end()
                context.detach(token)
                self.recorder.pop(sc.trace_id)   # drop the judge's spans; its cost is tracked separately

    def _scores(self, trace_id: str, root_id: str, grades, passed: bool) -> None:
        if self.lf is None:
            return
        try:
            for name, ok, detail in [*((g.name, g.passed, g.detail) for g in grades), ("overall", passed, "")]:
                post_score(self.lf, trace_id=trace_id, observation_id=root_id, name=name, passed=ok, comment=detail)
        except Exception as e:  # noqa: BLE001
            print(f"warning: could not write scores to Langfuse: {e}")


def post_score(lf, *, trace_id: str, observation_id: str, name: str, passed: bool, comment: str = "") -> None:
    """POST /api/public/scores directly. (The SDK's lf.create_score() is a silent no-op when the client was
    built with tracing_enabled=False, which we do because our traces go through OTel instead.)"""
    lf.api.scores.create(trace_id=trace_id, observation_id=observation_id, name=name, value=1 if passed else 0,
                         data_type="BOOLEAN", comment=comment[:1000] or None)


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(ROOT / "evals" / "config.yaml"))
    p.add_argument("--reps", type=int)
    p.add_argument("--variants", nargs="+", choices=VARIANTS)
    p.add_argument("--only", nargs="+", help="categories to run (e.g. lookup ranking)")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--no-langfuse", action="store_true")
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    cfg["reps"] = args.reps or cfg["reps"]
    cfg["variants"] = args.variants or cfg["variants"]
    if args.no_judge:
        cfg["judge"]["enabled"] = False

    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"   # graders need args + results
    load_eval_runner_secrets()
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = ROOT / "reports" / run_id
    out.mkdir(parents=True)

    print("building golden cases from live data (oracle)...")
    cases = await build_cases(seed=cfg["seed"])
    if args.only:
        cases = [c for c in cases if c.category in args.only]
    (out / "cases.json").write_text(json.dumps([c.to_dict() for c in cases], indent=2))

    lf = None if args.no_langfuse else langfuse_client()
    dataset_id = sync_dataset(lf, cases) if lf else None

    recorder = SpanRecorder()
    telemetry = setup_telemetry(environment="eval", extra_processors=[recorder])
    genai.instrument()

    runner = Runner(cfg, run_id, out, recorder, lf, dataset_id)
    total = len(cases) * cfg["reps"] * len(cfg["variants"])
    print(f"run {run_id}: {len(cases)} cases x {cfg['reps']} reps x {cfg['variants']} = {total} agent runs")
    await asyncio.gather(*(runner.run_one(v, c, r) for v in cfg["variants"] for c in cases
                           for r in range(1, cfg["reps"] + 1)))

    telemetry.shutdown()
    if lf:
        lf.flush()

    rows = sorted(runner.rows, key=lambda r: (r["variant"], r["case_id"], r["rep"]))
    with (out / "results.jsonl").open("w") as f:
        f.writelines(json.dumps(r, default=str) + "\n" for r in rows)
    with (out / "errors.jsonl").open("w") as f:
        f.writelines(json.dumps(e, default=str) + "\n" for e in runner.errors)
    summary = report.summarize(rows)
    gate_failures = report.check_gates(summary, cfg["gates"])
    if runner.errors and len(runner.errors) > 0.2 * total:
        gate_failures.append(f"{len(runner.errors)}/{total} runs hit infrastructure errors: results not trustworthy")
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    md = report.markdown(run_id, summary, rows, runner.errors, gate_failures)
    (out / "summary.md").write_text(md)
    (out / "junit.xml").write_text(report.junit(rows, runner.errors))
    print("\n" + md)
    print(f"reports: {out.relative_to(ROOT)}/")
    return 1 if gate_failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
