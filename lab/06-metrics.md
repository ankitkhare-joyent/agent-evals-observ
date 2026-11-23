# Lab 06 · Metrics: rates, errors and durations from spans

One sitting, about 30 minutes. Background: [docs/04](../docs/04-collector-pipeline.md) (spanmetrics) and
[docs/09](../docs/09-online-evals-and-monitoring.md) (SLOs). Config: `spanmetrics` in
[stack/otel-collector/config.yaml](../stack/otel-collector/config.yaml), [stack/prometheus/](../stack/prometheus).

Traces answer "what happened in *this* run". Metrics answer "how are we doing overall" cheaply enough to alert on.
Here none of them are written by hand: the Collector derives them from spans. **This lab calls Claude** twice,
about 4 cents. After it you can explain:

- where `agent_calls_total` and `agent_duration_milliseconds` come from, and why they lag traces
- the agent SLO queries: tool error rate, model calls per run, latency, token burn
- what a Collector restart does to counters, and why you always query with `rate()`/`increase()`

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
prom 'up'           # Prometheus scrapes the Collector twice: spanmetrics (:8889) and its own metrics (:8888)
```

The Collector restarted at the end of Lab 05, so its counters start from zero. Make two runs, one of them about an
instance that doesn't exist:

```bash
mise run -q trace -- "What is the largest instance running in account 567890123456?" | tail -2
mise run -q trace -- "What region is instance i-0000000000000dead in?" | tail -2
sleep 30     # spanmetrics flushes every 15 s, Prometheus scrapes every 15 s
```

## 1. Calls, by operation and status

**Predict (P1):** list the series you expect: which operations, which tools, and which will have
`status_code="STATUS_CODE_ERROR"`?

```bash
prom 'sum by (gen_ai_operation_name, gen_ai_tool_name, status_code) (agent_calls_total{gen_ai_agent_name!="lab-agent", gen_ai_tool_name!="echo"})'
```

## 2. The SLO queries

These are the queries behind [rules.yml](../stack/prometheus/rules.yml).

**Predict (P2):** the second question was answered politely ("I couldn't find it"). Did that run count as an
agent error? What is the tool error ratio?

```bash
prom 'sum by (gen_ai_tool_name) (increase(agent_calls_total{gen_ai_operation_name="execute_tool", status_code="STATUS_CODE_ERROR"}[10m]))
  / sum by (gen_ai_tool_name) (increase(agent_calls_total{gen_ai_operation_name="execute_tool"}[10m]))'
prom 'sum(agent_calls_total{gen_ai_operation_name="chat"}) / sum(agent_calls_total{gen_ai_operation_name="invoke_agent", gen_ai_agent_name="ec2-inventory-agent"})'
prom 'sum by (gen_ai_token_type) (gen_ai_client_token_usage_sum)'
prom 'histogram_quantile(0.95, sum by (le, gen_ai_operation_name) (agent_duration_milliseconds_bucket{gen_ai_agent_name!="lab-agent", gen_ai_tool_name!="echo"}))'
curl -s $PROM/api/v1/rules | jq -r '.data.groups[].rules[] | "\(.name)  \(.state)"'
```

**Predict (P3):** `AgentToolErrorRateHigh` fires when the ratio is above 10% *for 10 minutes*. What state is it in?

## 3. Where the series come from

**Predict (P4):** you made two agent runs. How many `agent_calls_total` series exist, and why does each new
`mise run trace` create more?

```bash
prom 'count(agent_calls_total)'
prom 'count(count by (service_instance_id) (agent_calls_total))'
curl -s "$PROM/api/v1/query" --data-urlencode 'query=agent_calls_total{gen_ai_operation_name="invoke_agent"}' \
  | jq -r '.data.result[0].metric | keys | join(" ")'
```

## Break it, then fix it

**Break: the Collector goes away.**

```bash
prom 'sum(agent_calls_total{gen_ai_operation_name="invoke_agent"})'
docker compose stop otel-collector
until [ "$(prom 'up{job="otel-collector"}' | awk '{print $NF}')" = "0" ]; do sleep 5; done
prom 'up{job="otel-collector"}'
prom 'sum(agent_calls_total{gen_ai_operation_name="invoke_agent"})'
```

**Predict (P5):** what does the last query return while the Collector is down, and what does a dashboard panel
built on it show?

**Fix:** start it, send one hand-made trace, and query again:

```bash
docker compose start otel-collector; sleep 5; mise run -q lab:span; sleep 35
prom 'sum(agent_calls_total{gen_ai_operation_name="invoke_agent"})'
prom 'sum(increase(agent_calls_total{gen_ai_operation_name="invoke_agent"}[15m]))'
```

**Predict (P6):** the raw counter was 2 before the outage. What is it now, and which of the two queries would you put
on a dashboard?

## Check your predictions

**P1.** Five series:

```
gen_ai_operation_name=chat                                       status_code=STATUS_CODE_UNSET  6
gen_ai_operation_name=execute_tool gen_ai_tool_name=find_instances   STATUS_CODE_UNSET  2
gen_ai_operation_name=invoke_agent                               status_code=STATUS_CODE_UNSET  2
gen_ai_operation_name=execute_tool gen_ai_tool_name=get_instance     STATUS_CODE_ERROR  1
gen_ai_operation_name=execute_tool gen_ai_tool_name=instance_history STATUS_CODE_ERROR  1
```

The "largest instance" run called `find_instances` twice; the missing instance made the agent try `get_instance`
and then `instance_history`, both of which returned "not found" as an MCP tool error. Three model calls per run.

**P2.** No: both `invoke_agent` spans are `UNSET` (OK). The model read the tool errors and answered ("If you know the
account or the name, I can search for similar instances."). Per tool, the error ratio is
`get_instance 1` and `instance_history 1` (100% of their calls), `find_instances` has no errors so it doesn't appear
in the numerator. Model calls per run: `3`. Tokens: `input 20055`, `output 1420` (input includes cache reads). p95:
`chat` about 8.5 s, `execute_tool` 95 ms, `invoke_agent` 19 s. These come from histogram buckets, so they're
bucket-boundary estimates, not exact values.

**P3.** `AgentToolErrorRateHigh  pending`: the condition is true, but it has to hold for 10 minutes before it fires.
That `for:` clause keeps one bad question from paging someone. The other two are `inactive`.

**P4.** `7` series from `2` processes. Each `mise run trace` is a new process with a new random `service.instance.id`,
and `resource_to_telemetry_conversion` turns every resource attribute into a Prometheus label
(`service_instance_id`, `telemetry_sdk_version`, …). For a long-running agent service that's fine. For thousands of
short CLI runs or batch jobs it is a cardinality problem: every run adds series that Prometheus keeps in memory.
Fixes: aggregate in the Collector (spanmetrics `resource_metrics_key_attributes`, or delete `service.instance.id`
before the connector), or keep the conversion off and join on `target_info` when you need resource labels. Always
query with `sum by (...)` over the labels you care about, as these queries do.

**P5.** `up 0`, and the query returns **nothing** (an empty result, not 0). When a scrape fails, Prometheus marks
the target's series stale, so a dashboard shows "No data" and an alert built on a ratio of these series silently
can't fire. That's why you also alert on `up == 0` and on absent data (`absent(agent_calls_total)`).

**P6.** `1`: the counter **reset**. The Collector keeps spanmetrics in memory, so a restart starts every series from
zero (the earlier runs' series are gone; only the new hand-made trace is counted). The raw `sum()` went 2 → 1, which
on a graph looks like traffic went *backwards*. `increase()` (and `rate()`) detect resets and add across them:
I got `15.05` over 15 minutes, counting every `invoke_agent` span since Lab 05, including the hand-made ones,
extrapolated to the window edges (hence the decimals; your number depends on how long the labs took). Dashboards
and alerts always use `rate()`/`increase()`, never the raw counter.

## What to remember

- Span metrics come for free from traces, before sampling; they flush on an interval, so they lag a little.
- A tool error is an SLO signal even when the agent recovers and the run looks fine.
- Short-lived processes multiply series: aggregate or drop per-instance labels.
- Counters reset on restart. Query with `rate()`/`increase()`, and alert on `up == 0` and absent data.

Next: [Lab 07 · Evals](07-evals.md).
