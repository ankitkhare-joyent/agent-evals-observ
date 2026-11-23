# 04 · The Collector pipeline

Apps could export straight to Langfuse. Production setups almost never do. They send OTLP to an
**OpenTelemetry Collector** (a sidecar, a DaemonSet per node, or a gateway Deployment), and the Collector
decides what happens next. Ours: [stack/otel-collector/config.yaml](../stack/otel-collector/config.yaml).

```
agent / eval runner ──OTLP/HTTP :34318──► otel-collector
                                         │
             traces/ingest: memory_limiter → redaction → transform/cap-sizes
                                         │
                         ┌───────────────┴────────────────┐
                    spanmetrics (connector)          forward/store (connector)
                         │                                │
             metrics: → Prometheus :8889       traces/store: tail_sampling → batch
                         │                                │
                    Prometheus :39095             otlphttp/langfuse ──► Langfuse
```

## Why a Collector, in one table

| Concern | Without a Collector | With one |
|---|---|---|
| Backend credentials | in every app | only in the Collector (here: rendered from OpenBao, lesson 10) |
| Switching or adding a backend | redeploy every app | edit one config |
| Redacting secrets/PII | trust every app | enforced centrally before data leaves |
| Sampling | head sampling only (decided before you know if the run failed) | **tail** sampling: keep every failed or slow trace |
| Metrics from traces | write them in code | `spanmetrics` derives RED metrics from spans |
| Backend outage | apps buffer or drop | queue + retry in one place |

## The components, in pipeline order

**receivers / `otlp`**: accepts gRPC (4317) and HTTP (4318). Published on 127.0.0.1:34317/34318.

**`memory_limiter`** always comes first: under a burst it refuses data (the SDK retries) instead of the
Collector being OOM-killed and losing everything in memory.

**`redaction`** masks values that look like secrets anywhere in span attributes: Anthropic keys,
Langfuse secret keys, AWS access key IDs, bearer tokens. Prompts and tool results are where secrets leak:
a user pastes a key, a tool returns a config file. Masking here is defence in depth; it doesn't replace
not capturing what you shouldn't.

**`transform/cap-sizes`** (OTTL language) truncates huge attribute values so one 5 MB tool result can't
break the backend's field limit or your storage bill.

**`spanmetrics` connector** turns spans into Prometheus metrics *before* sampling, so the numbers
stay exact even when you keep only 10% of traces:
`agent_calls_total` and `agent_duration_milliseconds_bucket`, labelled by
`gen_ai_operation_name`, `gen_ai_request_model`, `gen_ai_tool_name`, `status_code`. That is rate, errors
and duration for every model and every tool, with no extra code.

A *connector* is an exporter for one pipeline and a receiver for another. We use two: `spanmetrics`
(traces to metrics) and `forward/store`, which splits ingest from storage so sampling only affects
what's stored.

**`tail_sampling`** waits `decision_wait` (10s) for all spans of a trace, then decides per trace. Our
policies keep every error, every run over 30 s, and every eval run, then a probabilistic share of the
rest (100% locally; 5-20% is typical in production). Head sampling (deciding at the first span) would
throw away failures you only learn about at the end. One catch: tail sampling needs every span of a trace
on the same Collector instance; at scale you put a `loadbalancing` exporter in front, routing by trace ID.

**`batch`** groups spans into fewer, bigger requests.

**`otlphttp/langfuse`** sends to Langfuse's OTLP endpoint (`/api/public/otel`, HTTP only, no gRPC) with
`Authorization: Basic base64(public_key:secret_key)` and `x-langfuse-ingestion-version: 4` (without it,
Langfuse v4 can delay ingestion). The header value is read from a file:

```yaml
Authorization: ${file:/secrets/langfuse_otlp_authorization}
```

That file is rendered into a tmpfs volume by the `bao-agent-collector` sidecar from
`secret/langfuse/otlp` in OpenBao. The key is never in this config, the compose file or git.
`retry_on_failure` and `sending_queue` absorb a Langfuse restart; for zero loss across a Collector
restart you would add the `file_storage` extension as a persistent queue.

## Try it

```bash
curl -s localhost:39095/api/v1/query --data-urlencode \
  'query=sum by (gen_ai_tool_name) (agent_calls_total{gen_ai_operation_name="execute_tool"})' | jq
docker compose logs otel-collector | grep -i redact       # the redaction processor's summary
```

Prometheus UI: http://localhost:39095 (try the alert rules under *Alerts*; lesson 09 explains them).

Next: [05 · Langfuse, and how it compares](05-langfuse.md)
