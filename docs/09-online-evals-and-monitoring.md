# 09 · Online evals and monitoring

Offline evals tell you a change is safe to ship. Production tells you what users actually ask, which is
never quite your golden set. Two complementary tools watch it.

## 1. Metrics and alerts: cheap, on 100% of traffic

The Collector's `spanmetrics` connector and our instrumentation's GenAI metrics land in Prometheus
(http://localhost:39095). Useful queries:

```promql
# agent runs per minute, and p95 end-to-end latency
sum(rate(agent_calls_total{gen_ai_operation_name="invoke_agent"}[5m])) * 60
histogram_quantile(0.95, sum by (le) (rate(agent_duration_milliseconds_bucket{gen_ai_operation_name="invoke_agent"}[15m])))

# tool error rate, per tool
sum by (gen_ai_tool_name) (rate(agent_calls_total{gen_ai_operation_name="execute_tool", status_code="STATUS_CODE_ERROR"}[10m]))
  / sum by (gen_ai_tool_name) (rate(agent_calls_total{gen_ai_operation_name="execute_tool"}[10m]))

# tokens per minute, input vs output
sum by (gen_ai_token_type) (rate(gen_ai_client_token_usage_sum[5m])) * 60

# model calls per agent run (a rising number means the agent is looping)
sum(rate(agent_calls_total{gen_ai_operation_name="chat"}[30m])) / sum(rate(agent_calls_total{gen_ai_operation_name="invoke_agent"}[30m]))
```

The alert rules in [stack/prometheus/rules.yml](../stack/prometheus/rules.yml) encode three SLOs:
tool error rate above 10%, p95 run time above 60 s, and token burn at 3x the same hour yesterday (a prompt
regression, a loop, or abuse). In production these route to Alertmanager / PagerDuty like any other SLO.

What metrics cannot tell you: whether the answers were *right*. A wrong answer is a fast, cheap, 200 OK.

## 2. Online evaluation: judgement on a sample

[evals/online.py](../evals/online.py) runs on a schedule (Airflow DAG, Kubernetes CronJob, cron):

1. pull recent root `AGENT` observations from Langfuse's API (`environment` development/production),
2. sample deterministically by trace ID (re-runs never re-score a different subset),
3. skip traces that already have an `online_*` score,
4. rebuild question, answer and tool results from the trace,
5. run reference-free checks: grounded instance IDs (code) and the judge without a reference,
6. write `online_*` scores to the trace.

```bash
mise run trace -- "What is the largest instance running in account 392918273645?"   # make some traffic
mise run online-eval -- --hours 1 --sample 1.0
```

A Langfuse v4 detail that will bite you: v4 servers run in "events_only" mode, where several older read
endpoints (`GET /api/public/scores`, dataset runs) return 404. Use the v2 observations API and the v3
scores API, as this script does. And the SDK's `create_score()` silently does nothing on a client created
with `tracing_enabled=False`, so we post scores with `api.scores.create()` directly.

In Langfuse, filter traces by `online_judge_groundedness = 0`. Each one is a candidate eval case: read
it, decide what the right answer was, and add it to [evals/cases.py](../evals/cases.py) (or to a hand
verified list). Langfuse can also run LLM-judge evaluators server-side on incoming traces, configured in
the UI. We used our own script so you can see every step, and so the same judge code serves offline and
online.

## Other production signals worth wiring

- **User feedback**: thumbs up/down in the UI, posted as a score on the trace ID.
- **Implicit signals**: the user re-asks the same question, or abandons the session.
- **Cost per successful task**, not per request: a cheaper model that needs more turns isn't cheaper.
- **Drift**: the mix of question categories changes; new tools or data make old cases stale.

Next: [10 · Secrets with OpenBao](10-openbao-secrets.md)
