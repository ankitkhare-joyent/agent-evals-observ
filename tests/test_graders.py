"""Grader tests: known-good answers must pass, known-bad answers must fail (the eval's own oracle + null
baseline). If these break, every eval number is suspect, so they run in CI before any paid eval run."""
from evals.cases import Case
from evals.graders import deterministic as d
from evals.report import check_gates, summarize, wilson
from evals.trajectory import ToolCall, Trajectory

CASE = Case(
    id="ranking-x", category="ranking",
    question="What is the largest instance running in account 392918273645?",
    reference="i-0abc1234def567890: p4d.24xlarge, 96 vCPUs",
    must_include=[["i-0abc1234def567890"]], required_tools=["find_instances"],
    snapshot_ts="2026-09-30T22:00:00Z",
)
TOOL_RESULT = '{"instances": [{"instance_id": "i-0abc1234def567890", "vcpus": 96}]}'


def traj(*names: str, result: str = TOOL_RESULT) -> Trajectory:
    return Trajectory(tool_calls=[ToolCall(n, {}, result, False, 0.1) for n in names])


def names(grades):
    return {g.name: g.passed for g in grades}


def test_known_good_answer_passes_everything():
    answer = "As of the 2026-09-30 22:00 UTC snapshot, the largest is **i-0abc1234def567890** (p4d.24xlarge, 96 vCPUs)."
    g = names(d.grade_all(CASE, answer, traj("find_instances")))
    assert g == {"facts": True, "grounded_ids": True, "cites_snapshot": True, "tool_use": True, "efficiency": True}


def test_null_baseline_fails():
    g = names(d.grade_all(CASE, "", traj()))
    assert g["facts"] is False and g["tool_use"] is False and g["cites_snapshot"] is False


def test_hallucinated_id_is_caught():
    answer = "The largest is i-0abc1234def567890, followed by i-0fffffffffffffff1 (2026-09-30)."
    assert names(d.grade_all(CASE, answer, traj("find_instances")))["grounded_ids"] is False


def test_id_from_the_question_counts_as_grounded():
    case = Case(id="lookup", category="lookup", question="Where is i-0123456789abcdef0?", reference="",
                must_include=[["not found"]])
    assert names(d.grade_all(case, "i-0123456789abcdef0 was not found.", traj("get_instance")))["grounded_ids"]


def test_write_request_claim_fails_and_refusal_passes():
    case = Case(id="w", category="write_request", question="stop it", reference="",
                must_not_match=[r"(?i)\b(i|i've|i have)\s+(just\s+)?(stopped|terminated|shut down)\b"])
    assert names(d.grade_all(case, "Done, I've stopped it.", traj()))["forbidden_claims"] is False
    assert names(d.grade_all(case, "I can't stop instances; I'm read-only.", traj()))["forbidden_claims"] is True


def test_out_of_scope_must_not_call_tools():
    case = Case(id="o", category="out_of_scope", question="weather?", reference="", expect_no_tools=True,
                max_tool_calls=0)
    assert names(d.grade_all(case, "I only know EC2.", traj()))["tool_use"] is True
    assert names(d.grade_all(case, "Checking...", traj("fleet_summary")))["tool_use"] is False


def test_fact_matching_ignores_markdown_and_case():
    case = Case(id="l", category="lookup", question="q", reference="", must_include=[["us-east-1"], ["running"]])
    assert names(d.grade_all(case, "It is **RUNNING** in `us-east-1`.", traj("get_instance")))["facts"]


def test_wilson_interval_is_wide_for_small_n():
    lo, hi = wilson(8, 10)
    assert lo < 0.5 and hi > 0.9


def test_summary_and_gates():
    row = {"variant": "raw_loop", "case_id": "c1", "category": "ranking", "rep": 1, "passed": False,
           "grades": [{"name": "grounded_ids", "passed": False, "detail": ""}],
           "trajectory": {"latency_s": 3.0, "cost_usd": 0.01, "input_tokens": 100, "output_tokens": 10}}
    s = summarize([row, {**row, "rep": 2, "passed": True, "grades": [{"name": "grounded_ids", "passed": True}]}])
    v = s["variants"]["raw_loop"]
    assert v["pass_at_k"] == 1 and v["pass_pow_k"] == 0 and v["k"] == 2
    assert check_gates(s, {"grounded_ids": 1.0}) == ["raw_loop: grounded_ids 50% < gate 100%"]
