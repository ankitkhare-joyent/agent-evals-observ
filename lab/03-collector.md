# Lab 03 · The OpenTelemetry Collector: spans in, spans out

One sitting, about 30 minutes. Background: [docs/04](../docs/04-collector-pipeline.md). Config:
[stack/otel-collector/config.yaml](../stack/otel-collector/config.yaml). Script: [send_span.py](send_span.py).

Every span from the agent and the eval runner passes through the Collector, so this is where you look first when
"traces are missing". After this lab you can explain:

- how to tell, from the Collector's own metrics, whether data arrived, was dropped, or is waiting
- why a trace appears in Langfuse about 10 seconds after the run ends
- what redaction catches, and what the trace records about it
- which backend outages lose data and which don't

You don't need the agent for this lab: `mise run -q lab:span` sends a two-span trace (an `invoke_agent` root with
one `tools/call` child) straight to the Collector, with no Claude call.

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
curl -s localhost:34133 | jq -c '{status}'      # the Collector's health check extension
```

## 1. The Collector's own metrics

The Collector reports its own throughput on port 8888 (published as `$OTELCOL`). `otel_stats` keeps the counters
that matter: spans **accepted** by the receiver, traces **sampled** by tail sampling, spans **sent** (or failed)
by the Langfuse exporter.

```bash
otel_stats
```

**Predict (P1):** send one trace (2 spans). Right after it, will `accepted_spans` and `exporter_sent_spans` both
have gone up by 2? What about 15 seconds later?

```bash
TID=$(mise run -q lab:span); sleep 2; otel_stats | grep -E 'accepted_spans|exporter_sent'
sleep 13; otel_stats | grep -E 'accepted_spans|exporter_sent|traces_sampled'
```

## 2. Redaction

`send_span.py --leak` puts a fake Anthropic key and a bearer token into the tool span's result, the way a tool
that returns a config file or an error message with headers would.

**Predict (P2):** what will the tool's output look like in Langfuse? Will the span record that something was
removed?

```bash
TID=$(mise run -q lab:span -- --leak); sleep 18
lf_get api/public/v2/observations "traceId=$TID&fields=core,basic,io" | jq -r '.data[] | select(.type=="TOOL") | .output'
lf_get api/public/v2/observations "traceId=$TID&fields=core,metadata" | jq -c '.data[] | select(.type=="TOOL") | .metadata
  | with_entries(select(.key | startswith("attributes.")))'
```

Open [config.yaml](../stack/otel-collector/config.yaml) and find `blocked_values`.

**Predict (P3):** a user pastes their AWS *secret* access key (40 random characters, no prefix) into a question.
Is it masked?

## 3. The pipeline split

**Predict (P4):** in the config, `spanmetrics` sits in `traces/ingest` and `tail_sampling` sits in
`traces/store`. If you set the `everything-else` policy to 10%, which numbers change: the Prometheus metrics
(Lab 06), or what Langfuse stores, or both?

## Break it, then fix it

**Break: Langfuse goes down.** Stop its web container, send a trace, and watch the Collector:

```bash
docker compose stop langfuse-web
TID=$(mise run -q lab:span); echo "sent $TID"; sleep 25
docker compose logs --no-log-prefix --since 30s otel-collector | grep 'Exporting failed' | head -2 \
  | grep -oE '(Exporting failed[^"]*|"interval": "[^"]*")'
otel_stats | grep -E 'exporter_(sent|send_failed)'
```

**Predict (P5):** is the trace lost? Compare with the 401 in Lab 02.

**Fix:** start Langfuse again and wait:

```bash
docker compose start langfuse-web
until curl -sf localhost:33000/api/public/health >/dev/null; do sleep 3; done; sleep 40
lf_trace $TID
otel_stats | grep -E 'exporter_(sent|send_failed)'
```

**Predict (P6):** the outage lasted about a minute and nothing was lost. Name two ways it could have lost data
anyway, and the setting that addresses each.

## Check your predictions

**P1.** No. Two seconds after sending, `accepted_spans` went from 2 to 4 but `exporter_sent_spans` stayed at 2.
Fifteen seconds later it was 4, and `traces_sampled{decision="sampled"}` went from 1 to 2. `tail_sampling` holds
every trace for `decision_wait: 10s` so it can see all the spans before deciding, then the `batch` processor waits up
to 2 s more. That's why traces show up 10-15 s after a run ends, and why the labs say `sleep 18`.

**P2.** `config dump: ANTHROPIC_API_KEY=**** auth=****`: each matching substring is replaced, and the rest of the
value stays readable. The span's metadata gains `"attributes.redaction.masked.count": 1` (one *attribute* had
values masked, in this case two of them). That counter is what lets you find and fix the source, since the
Collector is the safety net, not the fix.

**P3.** No. `blocked_values` matches patterns: `sk-ant-…`, `sk-lf-…`, `AKIA…` (an access key *ID*), and bearer
tokens. A bare 40-character secret has no recognisable shape, and a regex broad enough to catch it would mangle
every hash and ID in your traces. Defences for that: don't capture message content in production
(`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`), redact by attribute *key* (the processor's `blocked_keys`), or
redact upstream where you know what a field contains.

**P4.** Only what Langfuse stores. `spanmetrics` sees every span in `traces/ingest`, before the
`forward/store` connector hands them to the sampled pipeline, so request rates, error rates and latency histograms
stay exact. Errors and slow runs are still kept at 100% by the earlier policies.

**P5.** Not lost. The exporter logged `Exporting failed. Will retry the request after interval.` with growing
intervals (`7.4s`, `6.6s`, ...; it's exponential backoff with jitter), `send_failed_spans` didn't move, and when the
web container came back the trace arrived: `AGENT invoke_agent lab-agent`, `TOOL echo`, and
`exporter_sent_spans` went from 6 to 8. A connection error is retryable; the 401 in Lab 02 was a *permanent*
error, so those spans were dropped immediately.

**P6.** (1) The outage outlasts the retry budget: `retry_on_failure.max_elapsed_time: 300s`, after which the batch
is dropped. Raise it, or put a durable buffer (Kafka, or the backend's own queue) in front. (2) The Collector
restarts or crashes while data sits in its queue: `sending_queue` is in memory. Add the `file_storage` extension and
set `sending_queue.storage` so the queue survives restarts. A third, under load: the queue fills up
(`queue_size: 2000` batches) and new data is refused; watch `otelcol_exporter_queue_size` against
`otelcol_exporter_queue_capacity`.

## What to remember

- `accepted` → `sampled` → `sent` / `send_failed`: three counters tell you where spans went.
- Traces reach the backend 10-15 s late on purpose: tail sampling needs the whole trace.
- Redaction is pattern-based and a safety net. The real fix is not capturing secrets in the first place.
- Connection errors are retried, auth errors are not, and the default queue lives in memory.

Next: [Lab 04 · Langfuse](04-langfuse.md).
