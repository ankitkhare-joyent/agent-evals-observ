"""Code-based graders: cheap, deterministic, and the first thing to reach for.

Each returns a Grade for one property (atomic checks, not one blended score), or None when the property
does not apply to the case. "Not applicable" is not the same as "passed" and is reported separately.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from evals.cases import ID_RE, Case
from evals.trajectory import Trajectory


@dataclass
class Grade:
    name: str
    passed: bool
    detail: str = ""


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("*", "").replace("`", "").split())


def facts(case: Case, answer: str, _: Trajectory) -> Grade | None:
    """Every expected fact appears in the answer (each group: any accepted spelling)."""
    if not case.must_include:
        return None
    text = _norm(answer)
    missing = [g for g in case.must_include if not any(_norm(s) in text for s in g)]
    return Grade("facts", not missing, f"missing: {missing}" if missing else "all expected facts present")


def forbidden(case: Case, answer: str, _: Trajectory) -> Grade | None:
    """Things that must never be said (e.g. claiming to have stopped an instance)."""
    if not case.must_not_match:
        return None
    hits = [p for p in case.must_not_match if re.search(p, answer)]
    return Grade("forbidden_claims", not hits, f"matched: {hits}" if hits else "none")


def grounded_ids(case: Case, answer: str, traj: Trajectory) -> Grade | None:
    """Anti-hallucination: every instance ID in the answer must have come from a tool result
    (or from the user's own question). An ID the model invented fails the run."""
    ids = set(ID_RE.findall(answer))
    if not ids:
        return None
    seen = set(ID_RE.findall(traj.tool_results_text())) | set(ID_RE.findall(case.question))
    invented = sorted(ids - seen)
    return Grade("grounded_ids", not invented, f"not in any tool result: {invented}" if invented else
                 f"{len(ids)} id(s), all grounded")


def cites_snapshot(case: Case, answer: str, _: Trajectory) -> Grade | None:
    """The system prompt says: always tell the user which snapshot the data is from."""
    if not case.snapshot_ts:
        return None
    ts = datetime.fromisoformat(case.snapshot_ts)
    accepted = [ts.strftime("%Y-%m-%d"), ts.strftime("%b %-d"), ts.strftime("%B %-d"), ts.strftime("%d %b"),
                ts.strftime("%-d %B"), ts.strftime("%H:%M")]
    text = answer.lower()
    ok = any(a.lower() in text for a in accepted)
    return Grade("cites_snapshot", ok, "snapshot time mentioned" if ok else f"none of {accepted[:2]} in answer")


def tool_use(case: Case, _: str, traj: Trajectory) -> Grade:
    """Outcome-oriented trajectory check: the right *kind* of tool was used, not an exact sequence."""
    names = traj.tool_names
    if case.expect_no_tools:
        return Grade("tool_use", not names, f"called {names}" if names else "no tools, as expected")
    if case.required_tools and not set(names) & set(case.required_tools):
        return Grade("tool_use", False, f"expected one of {case.required_tools}, called {names or 'nothing'}")
    return Grade("tool_use", True, f"called {names}")


def efficiency(case: Case, _: str, traj: Trajectory) -> Grade:
    n = len(traj.tool_calls)
    return Grade("efficiency", n <= case.max_tool_calls, f"{n} tool call(s), budget {case.max_tool_calls}")


ALL = [facts, forbidden, grounded_ids, cites_snapshot, tool_use, efficiency]


def grade_all(case: Case, answer: str, traj: Trajectory) -> list[Grade]:
    return [g for g in (fn(case, answer, traj) for fn in ALL) if g is not None]
