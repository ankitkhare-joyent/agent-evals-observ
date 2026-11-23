# 05 · Langfuse, and how it compares

## Why Langfuse here

Picked on four criteria: open source licence, production use, job-market demand, and native OTel ingest.

- **MIT licence**, fully self-hostable (only a few enterprise features sit in a separate `ee/` folder).
- The most widely deployed open source LLM-observability platform; in January 2026 **ClickHouse acquired
  Langfuse** (with a US$400M Series D for ClickHouse). The team says the licence and self-hosting stay as they
  are, and Langfuse already ran on ClickHouse, so the technical direction didn't change.
- Covers the whole loop in one tool: tracing, sessions, cost, **datasets and experiments**, scores
  (from code, LLM judges, humans, user feedback), annotation queues, prompt management.
- **OTLP endpoint** built in (`/api/public/otel`), mapping `gen_ai.*`, OpenInference (`llm.*`) and
  its own `langfuse.*` attributes. So our instrumentation stays vendor-neutral.

## Architecture (what each container in compose.yaml does)

```
OTLP / SDK / API ─► langfuse-web (Next.js: UI + public API + ingestion endpoint)
                          │  writes raw events to S3 (MinIO) and enqueues a job (Redis / BullMQ)
                          ▼
                    langfuse-worker (Node) ── reads the events, merges spans into observations,
                          │                     computes costs, runs evaluators, exports
                          ▼
     ClickHouse: traces, observations, scores (OLAP, columnar, huge write volume)
     Postgres:   users, projects, API keys, prompts, datasets, settings (OLTP)
     Redis:      queues and caches
     MinIO (S3): raw event blobs (the durable buffer; ClickHouse can be rebuilt from it) and media
```

You'll recognise the pattern from your platform: object storage as the durable log, a queue to decouple
ingestion from processing, a columnar store for analytics, a row store for metadata. v3 (Dec 2024)
introduced it; **v4** (2026) is what we run (`langfuse:4`, server 4.50 at the time of writing).

Things that bite on first start (we hit them all here):
- ~100 Postgres and ClickHouse migrations run on the first boot. On a busy laptop that takes 10-20 minutes.
- ClickHouse writes a lot of self-monitoring into `system.*` tables. We switch most of it off in
  [stack/clickhouse/low-resources.xml](../stack/clickhouse/low-resources.xml).
- `LANGFUSE_INIT_*` variables create the org, project, API keys and first user headlessly, so nobody
  clicks through a setup wizard and the keys can come from OpenBao.

## Querying traces with SQL

Since ClickHouse is the trace store, you can ask it anything with SQL (read-only, for exploration):

```bash
docker compose exec clickhouse sh -c 'clickhouse-client -u clickhouse --password "$(cat /secrets/clickhouse_password)"'
```
```sql
SHOW TABLES;                                   -- observations, traces, scores, events_*, ...
SELECT name, count(), round(avg(total_cost), 5) AS avg_cost
FROM observations WHERE project_id = 'ec2-agent' AND type = 'GENERATION'
GROUP BY name ORDER BY 2 DESC;
```

For anything programmatic use the public API (`/api/public/...`, Basic auth with the project keys) or
the Python SDK's `langfuse.api.*` client, as [evals/online.py](../evals/online.py) does. Table layouts
are internal and change between versions.

## The landscape (October 2026)

| Tool | Licence | Self-host | Strengths | Notes |
|---|---|---|---|---|
| **Langfuse** | MIT | yes, full | tracing + evals + prompts + datasets, OTel ingest | ClickHouse-owned since Jan 2026 |
| Arize **Phoenix** | Elastic License 2.0 | yes, single container | lightest to run, strong eval library, OpenInference | ELv2 is source-available, *not* OSI open source |
| Comet **Opik** | Apache-2.0 | yes (heavier stack) | evals, online rules, agent optimisation | similar architecture to Langfuse |
| **MLflow 3** | Apache-2.0 | yes, light | OTel-native tracing + `mlflow.genai.evaluate`, fits Databricks shops | strong if you already run MLflow |
| LangSmith | proprietary | enterprise only | deepest LangChain/LangGraph integration | high job-post count, but paid |
| Braintrust | proprietary | hybrid | eval-first workflow, polished UI | paid |
| Datadog / Grafana / Honeycomb | commercial | n/a | LLM views inside your existing APM | ingest the same OTel GenAI spans |

Evaluation *frameworks* (libraries, no server) you will see in job posts:
- **DeepEval** (Apache-2.0): pytest-style, 50+ metrics including agent/tool metrics.
- **Ragas**: RAG and agent metrics.
- **promptfoo** (MIT): declarative YAML evals and red-teaming in CI; joined OpenAI in March 2026,
  still MIT.
- **Inspect AI** (UK AI Security Institute): rigorous agent evals in sandboxes, used for safety research.

What we built is deliberately framework-free (plain Python graders and a judge) so you can see every
moving part. Each of those libraries packages the same ideas; with lessons 06-08 behind you, picking one
up takes an afternoon.

**What the job market wants**: the concepts (tracing, offline/online evals, LLM-as-judge with
calibration, regression gates in CI, cost and latency SLOs) plus at least one platform. Langfuse and
LangSmith appear most often for LLM-specific roles; OpenTelemetry appears everywhere.

Next: [06 · Eval fundamentals](06-eval-fundamentals.md)
