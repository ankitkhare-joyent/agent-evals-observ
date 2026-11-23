# 06 · Eval fundamentals

## What you can grade

| Grader type | Examples here | Cost | Use for |
|---|---|---|---|
| **Code** | expected facts present, every instance ID grounded in a tool result, no "I stopped it" claims, right tool family called | free, deterministic | anything checkable: IDs, numbers, formats, tool use, forbidden actions |
| **LLM judge** | correctness vs reference, groundedness, instruction following | ~1 cent per run | judgement calls: "is this the right answer, honestly stated?" |
| **Human** | labelled sample, annotation queue, user thumbs | expensive, slow | calibrating judges, ground truth for new failure modes |

Reach for code first. Use a judge only for properties code can't see, and check the judge against
humans (lesson 08).

## Outcome vs trajectory

- **Outcome**: is the final answer right? The thing users care about.
- **Trajectory**: did it get there sensibly: right tools, sane arguments, no loops, within budget?

Grade outcomes strictly and trajectories loosely. Requiring an exact tool sequence fails an agent that
found a different valid route (`fleet_summary` *or* `find_instances` both answer "which account has the
most vCPUs"). So our `tool_use` grader checks "at least one of these tools" and `efficiency` checks a
budget, but neither dictates order. Strict trajectory checks are right for **safety**: a write tool must
never be called; for an out-of-scope question, no tool should be called at all.

## Where the expected answers come from

Ground truth is the hardest part. Options, best first:

1. **Computed from the source of truth.** Our [evals/cases.py](../evals/cases.py) calls the inventory
   tools deterministically each run and derives the right answer. Data changes every 2 hours, so a
   hand-written key would go stale and start failing good agents.
2. **Human-written and verified**, for a stable core of cases.
3. **Model-generated, human-reviewed.** Never use a model's outputs as gold when you are comparing that
   model against another; you'd be grading imitation.

Also handle ties. "The largest instance" can be several instances with identical vCPUs and memory; the
case accepts any of them. Case bugs like that are a common reason a good agent "fails".

## Coverage in both directions

An eval for "does it use tools when it should" also needs "does it *not* use tools when it shouldn't",
otherwise always-call-tools scores perfectly. Our categories:

| Category | Tests | Must not |
|---|---|---|
| lookup, ranking, aggregation, history | answering from data | invent IDs or numbers |
| not_found | a nonexistent instance | make up a region |
| write_request | "stop instance X" | claim it did (it's read-only) |
| out_of_scope | weather question | call inventory tools or invent a forecast |
| invalid_input | 5-digit account ID | list another account's instances |

## Reliability: pass@k and pass^k

Run every case k times. Then:
- **pass@k**: the case passed at least once. Measures *capability*.
- **pass^k**: it passed every time. Measures *reliability*, which is what users experience.

An agent at 90% per-run success has pass^3 = 73% on that case. Agents with good averages and bad
pass^k feel flaky in production. Our report prints both.

## How much should you trust the number?

With n runs, the 95% confidence interval on a pass rate is wide:

| runs | observed 80% means the truth is roughly |
|---|---|
| 10 | 49-94% |
| 24 | 60-91% |
| 100 | 71-87% |

So a change from 80% to 85% on 24 runs is noise. The report prints Wilson intervals for every
metric. To detect smaller changes add reps (cheapest), add cases, or compare variants on the *same*
cases (paired comparison).

## Hygiene that keeps numbers honest

- **Infra errors are not model failures.** A timeout, a 529 or a crashed tool goes to `errors.jsonl`, not
  into the pass rate. If more than 20% of runs error, the gate fails with "results not trustworthy".
- **Run the real entry point.** We import `agents.raw_loop.ask` from the agent's repo, never a copy.
- **Keep full trajectories** for every run (results.jsonl), so a surprising score can be traced to a fact.
- **Test the graders**: a known-good answer must pass, an empty answer must fail
  ([tests/test_graders.py](../tests/test_graders.py)).
- **Read the failures.** If more than 1 in 10 "failures" are grader mistakes, fix the grader first.
- **Cost and latency are metrics too**, reported next to quality from real token counts.

Next: [07 · The eval suite, hands on](07-the-eval-suite.md)
