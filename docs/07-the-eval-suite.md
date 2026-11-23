# 07 · The eval suite, hands on

```bash
mise run eval                                      # 11 cases x 2 reps, ~$0.80 incl. judge
mise run eval -- --reps 1 --only ranking lookup    # iterate on one area
mise run eval -- --variants raw_loop langgraph     # same cases, two implementations
mise run eval -- --no-judge                        # free-ish: code graders only
```

## What happens, step by step ([evals/run.py](../evals/run.py))

1. **Secrets**: the runner logs into OpenBao with its own AppRole and reads the Anthropic and Langfuse keys
   into its process environment. Nothing on disk.
2. **Golden cases**: [evals/cases.py](../evals/cases.py) asks the MCP tools for today's facts and builds
   11 cases with expected answers (lesson 06). Saved as `reports/<run>/cases.json`.
3. **Dataset sync**: cases are upserted into the Langfuse dataset `ec2-agent-golden`.
4. **Instrument**, then for every (variant, case, rep), with at most 2 in flight:
   - open a root span `eval <category>` in a *fresh* trace, carrying `langfuse.experiment.*`
     attributes and baggage, so Langfuse files the trace under this experiment run and dataset item;
   - run the agent's real entry point inside `invoke_agent`, with a hard timeout;
   - rebuild the **trajectory from the spans** ([evals/trajectory.py](../evals/trajectory.py));
   - run the code graders, then the LLM judge (its spans are tagged as an *evaluator* observation and
     its cost is kept separate);
   - write every grade to Langfuse as a BOOLEAN score on the root observation.
5. **Report**: `results.jsonl` (every run with answer, grades, full trajectory, agent stdout),
   `errors.jsonl`, `summary.md`, `summary.json`, `junit.xml`. Exit code 1 if a gate in
   [evals/config.yaml](../evals/config.yaml) fails.

`overall` passes when every grade passes except two soft ones (`efficiency` and
`judge_instruction_following`), which are reported but don't fail a run.

## What the first real run found (October 4, 2026)

This is worth reading closely, because it is what evals are like in practice.

| Finding | Was it the agent? | What we changed |
|---|---|---|
| 9 of 11 runs failed with `httpx.ReadError` | No. The lakehouse API and MCP server were restarted mid-run | Nothing. The harness put them in `errors.jsonl`, not the pass rate, and the gate said "not trustworthy" |
| `lookup` case: "missing: running" | **No.** The answer said *terminated*, and it was right | The data was re-seeded during the run (snapshot moved 09-04 → 09-25). Added a **stale ground truth** guard: if the agent saw a newer snapshot than the oracle, the run is not scored |
| `ranking` cases: "missing instance ID" | **No.** 39 instances tied for largest; the agent said so and named the type | The case was ambiguous. With more than 3 ties the case now requires the type and vCPU count instead of an ID |
| `invalid_input` rep 2: judge failed all three criteria | **Yes.** For account "12345" it claimed that ID "matches several accounts" | A real agent bug, caught by the judge and missed by code graders. Rep 1 handled it fine: that's what pass^k exposes |
| judge_groundedness on ranking: answer asserted "all 39 are in us-east-1 and ap-northeast-1" after seeing only 50 of 68 rows | **Yes** | Real, and it repeats: the agent generalises past the page it read (3 of 4 ranking runs in a later check). A prompt or tool fix candidate (`find_instances` could return aggregate facts) |
| Second run: judge failed `ranking` for account 567890123456 twice, saying the agent was right and *the reference* was wrong | **No.** The oracle was wrong | The oracle took row 0 of a vCPU-sorted 50-row page, so it missed the memory tie-break (r7g.4xlarge, 128 GiB, beats c7g.4xlarge, 32 GiB). It now uses per-type totals from `fleet_summary`, exact over every instance. A clearly good answer "failing" is the classic sign of a bad case |

So, of the first batches of "failures", most were the eval's fault or the environment's, and two
patterns were genuine agent issues. That ratio is normal for a new suite, which is why lesson 06 says to read failures
before trusting a number. Reproducibility tip from your own world: pin evals to a fixed **Iceberg
snapshot** (time travel) so the data cannot move under a run; we can't here without changing the
agent's API, so we detect drift instead.

After those fixes the full run passed the gate: 82% overall (95% CI 61-93%), facts and grounded IDs
100%, pass@2 91% vs pass^2 73%, p50 latency 5.6 s, $0.023 per agent run plus ~$0.009 of judging.
The full report is in [example-eval-report.md](example-eval-report.md).

## Reading results in Langfuse

- **Datasets → ec2-agent-golden**, experiment runs: one per eval run (linked purely through the
  `langfuse.experiment.*` OTel attributes); open two to compare scores per item.
- **Traces**, filter environment = `eval`: every agent run, with scores on the right and the judge's
  reasoning in each score's comment.
- **Scores / dashboards**: pass rate per score name over time, to watch for drift.

## Making it a CI gate

[.github/workflows/evals.yml](../.github/workflows/evals.yml) shows the shape: grader unit tests on every
push (free, fast), the paid eval suite on PRs touching the agent, OIDC to OpenBao for keys, the report
uploaded as an artifact, and `junit.xml` so each case shows up as a test. Gates are deliberately strict
only where one failure matters (hallucinated IDs, claiming to have acted) and looser on averages,
because with ~22 runs the confidence interval is ±15 points (lesson 06).

Next: [08 · LLM-as-judge, done properly](08-llm-as-judge.md)
