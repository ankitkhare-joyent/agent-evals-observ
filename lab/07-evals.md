# Lab 07 · The eval suite, end to end

One sitting, about 45 minutes. Background: [docs/06](../docs/06-eval-fundamentals.md),
[docs/07](../docs/07-the-eval-suite.md). Code: [evals/run.py](../evals/run.py), [evals/cases.py](../evals/cases.py),
[evals/graders/](../evals/graders), [evals/report.py](../evals/report.py).

**This lab calls Claude**: six agent runs plus six judge calls, about 16 cents. After it you can explain:

- where the expected answers come from, and why they're computed instead of written down
- how one eval run turns into a report, a CI exit code, a JUnit file, and an experiment in Langfuse
- how to tell a grader bug from an agent bug, and why grader tests need negative examples

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
```

## 1. The graders, without the agent

```bash
mise exec -- uv run pytest -q tests/test_graders.py
```

Open [tests/test_graders.py](../tests/test_graders.py). Each test feeds a grader a known answer.

## 2. The golden cases

The oracle in [evals/cases.py](../evals/cases.py) calls the same MCP tools as the agent, with known-correct
arguments, and turns the results into cases. No Claude involved.

```bash
mise exec -- uv run python -c "
import asyncio
from evals.cases import build_cases
for c in asyncio.run(build_cases(seed=7)):
    print(f'{c.category:14} {c.id:26} tools={c.required_tools or (\"none\" if c.expect_no_tools else \"any\")} must={c.must_include}')
" | cut -c1-150
```

**Predict (P1):** the case IDs are hashes of the inputs plus the snapshot time. If you ran this tomorrow, would the
IDs be the same? Which categories have no `must` facts at all, and how are they graded?

## 3. A small run

Run five categories, one repetition each:

```bash
mise run -q eval -- --reps 1 --only lookup not_found write_request out_of_scope invalid_input; echo "exit=$?"
```

**Predict (P2):** six runs. Which do you expect to fail, and on which grader? What exit code?

## 4. Reading the results

```bash
R=$(ls -d reports/*/ | tail -1); ls $R
jq -c '{case: .case_id, passed, tools: [.trajectory.tool_calls[] | "\(.name)\(if .is_error then "(err)" else "" end)"],
        calls: .trajectory.model_calls, cost: .trajectory.cost_usd, s: .trajectory.latency_s}' $R/results.jsonl
grep -o 'tests="[0-9]*" failures="[0-9]*" errors="[0-9]*"' $R/junit.xml
lf_get api/public/experiments "fromStartTime=$(date -u +%Y-%m-%dT00:00:00Z)" | jq -c '.data[] | {name, itemCount}'
lf_scores $(jq -r 'select(.case_id|startswith("not_found")) | .trace_id' $R/results.jsonl)
```

Open `$R/summary.md` and read the judge's reasoning for each failure. Then open one failed trace in the Langfuse
UI (`mise run langfuse-login` for the password): the scores and their comments are on the right.

**Predict (P3):** for each failure, is it the agent's fault, the case's, or the grader's?

## Break it, then fix it

**Break: a grader that can't fail.** Make the `facts` grader pass everything, the kind of bug a refactor
introduces:

```bash
sed -i '' 's/return Grade("facts", not missing,/return Grade("facts", True,/' evals/graders/deterministic.py
git diff --stat
mise exec -- uv run pytest -q tests/test_graders.py 2>&1 | grep -E 'FAILED|passed|failed'
```

**Predict (P4):** which test catches it? Would `test_known_good_answer_passes_everything` alone have caught it?

**Fix:**

```bash
git checkout evals/graders/deterministic.py && mise exec -- uv run pytest -q tests/test_graders.py | tail -1
```

## Check your predictions

**P1.** Yes, as long as the data doesn't change: I got exactly the same IDs on this freshly reset stack as in the
first runs a week earlier (`aggregation-13567f735d`, `ranking-a85940477d`, …), because the lakehouse regenerates
the same synthetic data and the oracle picks with a fixed seed. When the snapshot moves, the IDs change and so do the
expected answers. Four categories have no `must` facts: `history`, `write_request`, `invalid_input`, and `out_of_scope`
(which is checked by "no tools called"). They rely on the forbidden-claims regexes, the tool checks
and the LLM judge with the reference answer. The `not_found` case has a long list of accepted phrasings, because
"couldn't find", "doesn't exist" and "no record" all mean the same thing (a grader that's too strict is a grader bug).

**P2.** Two failures, exit code `1` because the gate failed (`overall 67% < gate 80%`, `judge_groundedness 67% < 90%`):

```
PASS   lookup-83b63b03e4         4.2s $0.0184 1 tools
PASS   lookup-f803bebe50         5.5s $0.0189 1 tools
PASS   write_request-106696670a  7.8s $0.0227 1 tools
FAIL   not_found-f2056a3a9e      7.2s $0.0218 2 tools  failed: judge_groundedness, judge_instruction_following
PASS   out_of_scope-f98669cc9b   2.6s $0.0130 0 tools
FAIL   invalid_input-999d5a312d  7.9s $0.0269 1 tools  failed: judge_correctness, judge_groundedness, judge_instruction_following
```

Agent cost $0.122, judge $0.039. Every code grader passed: facts, grounded IDs, tool use, forbidden claims.
Only the judge saw the problems.

**P3 and step 4.** `results.jsonl` shows the trajectories: `invalid_input` called `fleet_summary` once;
`not_found` called `get_instance(err)` and `instance_history(err)`; `out_of_scope` called nothing; every run used
1-3 model calls. JUnit says `tests="6" failures="2" errors="0"`. Langfuse lists the run as an experiment,
`{"name":"golden-20261011-124947-b4bf","itemCount":6}`, linked to the dataset only through OTel attributes. The
`not_found` trace carries all eight scores (`facts true`, `grounded_ids true`, `judge_groundedness false`, …,
`overall false`).

- `invalid_input` (account "12345"): **the agent's fault**, and the same failure as in [docs/07](../docs/07-the-eval-suite.md).
  It said 12345 isn't a valid ID but then listed accounts "containing 12345", which several don't. A real, repeating
  bug.
- `not_found`: **arguable**. The answer was right (the instance doesn't exist) but didn't mention a snapshot time,
  and the judge read "not found in the last 30 days" vs "never seen" as an unsupported claim. Two careful people
  could disagree here, which is what judge calibration (Lab 08 and [docs/08](../docs/08-llm-as-judge.md)) is for. If
  you'd call it a pass, the fix is in the rubric, not the agent.

**P4.** Only `test_null_baseline_fails` (`assert (True is False)`): it feeds an empty answer and expects `facts`
to fail. The known-good test passes with the broken grader, as would every real run: the suite would have reported
100% facts forever. Grader tests need cases that *must fail*, not just cases that must pass. The fix restores the
file and `9 passed`.

## What to remember

- Gold comes from the data via an oracle, so it moves when the data moves and stays the same when it doesn't.
- One run produces a CI exit code, JUnit, a markdown report, full trajectories and a Langfuse experiment.
- Code graders catch the checkable; the judge catches "technically true but misleading". Read every failure and
  decide whose fault it is before trusting a number.
- A grader without a must-fail test can silently stop grading.

Next: [Lab 08 · The judge and online evals](08-judge.md).
