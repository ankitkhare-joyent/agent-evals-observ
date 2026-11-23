> Example output of `mise run eval` from October 4, 2026, kept here because reports/ is gitignored. Discussed in [lesson 07](07-the-eval-suite.md). One finding in it (ranking-a85940477d) turned out to be an oracle bug, since fixed.

# Eval run `20261004-131659-fda0`

**Gate: passed**

## Variant `raw_loop` (22 scored runs)

| metric | pass rate | 95% CI | passed / n |
|---|---|---|---|
| cites_snapshot | 100% | 76%-100% | 12 / 12 |
| efficiency | 100% | 85%-100% | 22 / 22 |
| facts | 100% | 78%-100% | 14 / 14 |
| forbidden_claims | 100% | 34%-100% | 2 / 2 |
| grounded_ids | 100% | 78%-100% | 14 / 14 |
| judge_correctness | 86% | 67%-95% | 19 / 22 |
| judge_groundedness | 91% | 72%-97% | 20 / 22 |
| judge_instruction_following | 95% | 78%-99% | 21 / 22 |
| overall | 82% | 61%-93% | 18 / 22 |
| tool_use | 100% | 85%-100% | 22 / 22 |

- Reliability over k=2 reps: pass@k 91%, pass^k 73%
- Latency p50 5.6s, p95 15.0s
- Agent cost $0.511 total, $0.0232 per run (140,769 tokens in, 11,544 out); judge $0.190

| category | passed / n |
|---|---|
| aggregation | 4 / 4 |
| history | 2 / 2 |
| invalid_input | 1 / 2 |
| lookup | 4 / 4 |
| not_found | 2 / 2 |
| out_of_scope | 2 / 2 |
| ranking | 1 / 4 |
| write_request | 2 / 2 |

## Failed runs

### raw_loop / invalid_input-999d5a312d (rep 2)
> List the running instances in account 12345.

- **judge_correctness**: The reference says 12345 is invalid and the assistant should say so and not list instances from another account. The answer does say it's invalid (5 digits vs 12), asks for the full ID, and does not list instances. However, it presents a table of other accounts with counts, which is borderline listing data from other accounts. It also claims several accounts have those digits in their ID: 789012345678 contains '12345' (7890 12345 678 yes), 567890123456 contains '12345'? 5678 90123456: '90123456' has '12345'? 9-0-1-2-3-4-5-6 yes contains 12345. 901234567890 contains 12345 (9 0 1 2 3 4 5 ...) yes. 456789012345 ends in 12345 yes. So claims are true. Also labels counts as 'running instances' while tool results only give instance counts grouped by account, not state; that is unsupported. It also omits 345678901234 which contains '1234' but not 12345, fine. Overall it doesn't list instances from another account but the table is misleading and speculative.
- **judge_groundedness**: The tool results give per-account 'instances' counts, with no indication they are running only. The answer labels them 'Running instances', which is an unsupported claim. The numbers themselves match the tool results. The substring claims are correct.
- **judge_instruction_following**: It states the snapshot time and explains the problem plainly. But it is not very short, and guessing candidate accounts by substring matching and presenting data for unrelated accounts goes beyond what's needed; rules say never guess accounts. It does ask for clarification rather than assuming, which is fine.
- trace: http://localhost:33000/project/ec2-agent/traces/6698a5ac2adef6719e0a4c7e282b3800

### raw_loop / ranking-7dacf1a83e (rep 2)
> What is the largest instance running in account 392918273645?

- **judge_groundedness**: Most claims are supported by the tool results: the 53 count, the specs, the listed instances, their regions and launch times, and the i4i.2xlarge count of 46 with 8 vCPUs and 64 GiB. However, 'All of them are on-demand eks-general nodes owned by the platform team' is not supported for all 53. Only 5 m6i.4xlarge instances were seen, so it generalizes beyond the data. Also, 'next biggest are the 46 i4i.2xlarge kafka-broker instances' is only partly supported. The 10-row sample shows kafka-broker only for i4i.2xlarge, but the rest are unseen. m6i.2xlarge has 8 vCPUs and 32 GiB, so i4i.2xlarge with 64 GiB is plausibly next, but 'all kafka-broker' is an extrapolation. Also, 'running' for all 53 is assumed from the group count. These overgeneralizations are unverified claims.
- trace: http://localhost:33000/project/ec2-agent/traces/0645c18f700459f24d4df6fa1d1414c6

### raw_loop / ranking-a85940477d (rep 1)
> What is the largest instance running in account 567890123456?

- **judge_correctness**: The reference says the largest is c7g.4xlarge (16 vCPUs, 32 GiB) with 22 tied. The reference defines largest by vCPU/memory ordering evidently by... it says c7g.4xlarge largest, while r7g.4xlarge has the same 16 vCPUs but 128 GiB memory, which is objectively larger by memory. The answer names r7g.4xlarge, contradicting the reference's key facts (type and IDs). By the grading rule, key facts must agree with the reference. The answer's claim is defensible from the tool data but disagrees with the reference's IDs/type. Under the instruction, this fails.
- trace: http://localhost:33000/project/ec2-agent/traces/c8e64ba913f037c37821bd4c3bd71229

### raw_loop / ranking-a85940477d (rep 2)
> What is the largest instance running in account 567890123456?

- **judge_correctness**: The reference says the largest is c7g.4xlarge (16 vCPUs, 32 GiB), with 22 tied. The answer instead says r7g.4xlarge (16 vCPUs, 128 GiB) is largest. Per the tool results, r7g.4xlarge has the same vCPUs and more memory, so the answer is defensible against the tool data. But it conflicts with the reference's key facts (type c7g.4xlarge). The reference's definition of 'largest' is evidently by vCPU, with ties broken by nothing. The answer's memory-based ranking is arguably reasonable, and it mentions both types. Still, the headline answer disagrees with the reference, so I grade it a fail on strict agreement with the reference.
- trace: http://localhost:33000/project/ec2-agent/traces/b80acc36e2524e48136fb913a652bed4

