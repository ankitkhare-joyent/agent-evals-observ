# Hands-on lab: the observability and eval stack, one component per sitting

The lessons in [docs/](../docs) explain the design. This track has you poke each running piece until you could
debug it yourself. It follows the data: secrets first (nothing starts without them), then how they reach the
containers, the telemetry pipeline (Collector, then Langfuse) tested with hand-made spans, then real agent spans,
metrics, and finally the evals and the judge that sit on top.

## Fresh start (do this once, before Lab 01)

```bash
cd ~/workstuff/agent-evals-observ
docker ps --format '{{.Names}}' | grep -v ec2-lakehouse    # should print nothing: one lab stack at a time
mise run agent-up        # the agent under test: the learn-ai-agent lakehouse + API + MCP (the shared exception)
curl -s localhost:8000/readyz | jq -c '{ready, rows}'   # want ready:true. rows:0 means an empty lakehouse:
                                                         #   (cd ../learn-ai-agent && mise run seed)
mise run reset           # DELETES this stack's volumes, .bao/ and reports/. Your ./.env (Anthropic key) stays
mise run setup           # Python deps + new OpenBao key material in .bao/
mise run up              # OpenBao, seed secrets, then everything else. First start: ~10 minutes (Langfuse migrations)
source lab/env.sh        # once per terminal
```

`mise run up` re-reads `ANTHROPIC_API_KEY` from `./.env` into OpenBao, so the labs that call Claude work straight
away. When you finish a sitting, `mise run down` stops the stack and keeps its data.

Every lab has the same shape:

1. **Look**: real commands against your running stack.
2. **Predict** before you run: each step asks a question (P1, P2, …). Write your guess down first. The answers, with
   the output I got running every command on a freshly reset stack, are at the bottom of each file.
3. **Break it, then fix it**: at least one failure on purpose, so you've seen it before it happens for real.

A tmux split works well: the lab on one side, a shell on the other, a third pane for `docker compose logs -f <svc>`.

| Lab | Component | You'll be able to explain | Changes anything? |
|---|---|---|---|
| [01](01-openbao.md) | OpenBao | self-initialization, auto-unseal, who can read what, the audit log | issues a new AppRole secret_id |
| [02](02-secret-delivery.md) | Sidecars and tmpfs | how a password reaches Postgres without being in any config; rotating and rolling back | writes and rolls back a secret version |
| [03](03-collector.md) | OTel Collector | spans in vs out, the 10-second delay, redaction, retries when the backend is down | no |
| [04](04-langfuse.md) | Langfuse | web vs worker, the Redis queue, traces in ClickHouse, keys in Postgres | no |
| [05](05-spans.md) | Instrumentation | one agent run as spans; tokens, cache and cost; what happens when telemetry is down | calls Claude (about 10 cents) |
| [06](06-metrics.md) | Prometheus | RED metrics from spans, the agent SLO queries, counter resets | calls Claude (about 4 cents) |
| [07](07-evals.md) | Eval suite | golden cases from an oracle, graders, reports, the CI gate | calls Claude (about 16 cents) |
| [08](08-judge.md) | LLM judge and online evals | rubric verdicts, prompt injection, scoring real traces, judge failures | calls Claude (about 10 cents) |
| [09](09-dynamic-secrets.md) | OpenBao dynamic secrets | a database user that exists for an hour, leases, revocation | creates and drops temporary Postgres users |

Each lab is one sitting of 30-45 minutes. Do them in order: later labs reuse things you saw earlier. All together
they spend about 40 cents of Claude API credit.

## Helpers

| | What | Where |
|---|---|---|
| `source lab/env.sh` | `$EVALS_BAO_ADDR`, `$EVALS_BAO_TOKEN` (admin, 1 hour), `$LF`, `$PROM`, `$OTELCOL`, and the functions below | [env.sh](env.sh) |
| `bao_keys PATH` | a secret's field names and version, never the values | |
| `bao_field PATH FIELD` | one secret value, for use inside `$( … )` only | |
| `approle_token ROLE` | log in as a workload; then `TOKEN=… bao_keys PATH` runs as it | |
| `lf_trace ID`, `lf_last`, `lf_scores ID` | a trace's observations, the newest agent trace, a trace's scores | |
| `ch "SQL"` | SQL on Langfuse's ClickHouse | |
| `prom 'PromQL'` | an instant Prometheus query | |
| `otel_stats` | spans accepted, refused and sent by each Collector component | |
| `mise run -q lab:span [-- --leak]` | one hand-made trace through the Collector (Labs 02-04) | [send_span.py](send_span.py) |
| `mise run -q lab:judge -- CASE` | the LLM judge on one answer (Lab 08) | [judge_once.py](judge_once.py) |

None of the labs print a secret value.

## If something is off

- `lab/env.sh: admin login failed`: the stack isn't up, or `.bao/` was reset after OpenBao started. `mise run up`.
- `lf_*` return nothing right after a run: Langfuse ingests asynchronously and the Collector waits 10 s to sample a
  trace. Give it 15-20 seconds.
- Agent questions time out, or fail with a 422 from `/auth/token`: the lakehouse is down (`mise run agent-up`), or
  something in your shell exported `BAO_ADDR`/`BAO_TOKEN` (Lab 05 explains why that breaks the agent).
- Your numbers differ from the answers: trace IDs, tokens, costs and timings change on every run, and the lakehouse
  data moves on as the learn-ai-agent stack runs. The shape and the reasoning should match. Anything else is worth a look.

## The stack, with the lab that covers each part

```
                     you (terminal)
                           │ mise run trace / eval / online-eval
                           ▼
   ┌───────────────────────────────────────────────┐         Claude API
   │ 05 instrumentation (same process as the agent)│◄───────► agent: Opus 5.5
   │    invoke_agent › chat · tools/call spans     │          judge: Sonnet 5.5  ◄── 08
   │ 07 eval runner · 08 judge + online evals      │
   └──────────┬───────────────────────┬────────────┘
              │ OTLP :34318           │ MCP → API → Iceberg (learn-ai-agent, unchanged)
              ▼                       ▼
   ┌────────────────────────────┐   the agent under test
   │ 03 OTel Collector          │
   │  redaction · spanmetrics   │───────► 06 Prometheus :39095 (RED metrics, alerts)
   │  tail_sampling · otlphttp  │
   └──────────┬─────────────────┘
              │ OTLP + Basic auth
              ▼
   ┌────────────────────────────────────────────────────┐
   │ 04 Langfuse :33000  web ─► MinIO (events) + Redis  │
   │                     worker ─► ClickHouse (traces)  │
   │                     Postgres (keys, datasets)      │
   └────────────────────────────────────────────────────┘

   ┌────────────────────────────────────────────────────┐
   │ 01 OpenBao :38200   self-init · auto-unseal · KV   │
   │ 02 sidecars ─► tmpfs ─► Collector, Langfuse, DBs   │
   │ 09 database engine ─► 1-hour Postgres users        │
   └────────────────────────────────────────────────────┘
     feeds every box above: DB passwords, Langfuse keys, the Anthropic key
```
