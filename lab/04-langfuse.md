# Lab 04 · Langfuse: what happens after the Collector sends a span

One sitting, about 30 minutes. Background: [docs/05](../docs/05-langfuse.md). Config: the `langfuse-*`,
`clickhouse`, `postgres`, `redis` and `minio` services in [compose.yaml](../compose.yaml).

Langfuse is five containers that look like one product. After this lab you can explain:

- what each of web, worker, ClickHouse, Postgres, Redis and MinIO holds
- why "the Collector sent it" and "I can see it in Langfuse" are two different facts
- why a worker outage loses nothing

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
docker compose ps --format '{{.Service}}\t{{.Status}}' | grep -E 'langfuse|clickhouse|postgres|redis|minio'
```

So far the only traces are the hand-made lab spans from Labs 02 and 03 (no agent runs yet).

## 1. Where the traces are

**Predict (P1):** Langfuse v4 keeps traces in ClickHouse. In Labs 02 and 03 you sent six two-span traces, and one
of them hit the bad key. How many rows are in the trace tables, and which table?

```bash
ch "SHOW TABLES"
for t in events_full events_core observations traces; do echo "$t: $(ch "SELECT count() FROM $t" | grep -oE '[0-9]+ ' | tail -1)"; done
ch "SELECT type, name, count() AS n FROM events_core WHERE project_id='ec2-agent' GROUP BY type, name ORDER BY type"
```

## 2. What lives in Postgres

**Predict (P2):** if someone copied Postgres's data, could they use the Langfuse API key in it?

```bash
docker compose exec -T postgres psql -U postgres -tAc "select id, name from projects;
  select left(public_key, 14) || '...', left(hashed_secret_key, 7) || '...' from api_keys;
  select count(*) || ' datasets' from datasets;"
```

## 3. The ingestion path: MinIO and Redis

The web container doesn't write traces to ClickHouse. It saves each OTLP request to S3 (MinIO) and puts a job on a
Redis queue (BullMQ). The worker takes the job, reads the file, and writes ClickHouse.

**Predict (P3):** how many objects are in the `langfuse` bucket?

```bash
docker compose exec -T minio sh -c 'mc alias set local http://localhost:9000 minio "$(cat /secrets/minio_password)" >/dev/null
  echo "objects: $(mc ls --recursive local/langfuse/ | wc -l)"; mc ls --recursive local/langfuse/ | head -2'
docker compose exec -T redis sh -c 'export REDISCLI_AUTH="$(sed -n "s/^requirepass //p" /secrets/redis.conf)"
  redis-cli --scan --pattern "bull:*" | sed -E "s/^(bull:[^:]+).*/\1/" | sort | uniq -c | sort -rn | head -8'
```

## Break it, then fix it

**Break: the worker stops.** Stop it, send a trace, and look at every layer:

```bash
docker compose stop langfuse-worker
TID=$(mise run -q lab:span); echo "sent $TID"; sleep 20
otel_stats | grep exporter_sent_spans
docker compose exec -T minio sh -c 'mc alias set local http://localhost:9000 minio "$(cat /secrets/minio_password)" >/dev/null
  echo "minio objects: $(mc ls --recursive local/langfuse/ | wc -l)"'
docker compose exec -T redis sh -c 'export REDISCLI_AUTH="$(sed -n "s/^requirepass //p" /secrets/redis.conf)"
  echo "queue waiting: $(redis-cli llen bull:otel-ingestion-queue:wait)"'
echo "clickhouse rows: $(ch "SELECT count() FROM events_core" | grep -oE '[0-9]+ ' | tail -1)"
lf_trace $TID | wc -l
```

**Predict (P4):** which of these went up: the Collector's sent count, the MinIO objects, the Redis queue,
the ClickHouse rows, the API result?

**Fix:** start the worker and look again:

```bash
docker compose start langfuse-worker; sleep 30
docker compose exec -T redis sh -c 'export REDISCLI_AUTH="$(sed -n "s/^requirepass //p" /secrets/redis.conf)"
  echo "queue waiting: $(redis-cli llen bull:otel-ingestion-queue:wait)"'
echo "clickhouse rows: $(ch "SELECT count() FROM events_core" | grep -oE '[0-9]+ ' | tail -1)"
lf_trace $TID
```

**Predict (P5):** what would have happened if Redis had been wiped while the worker was down? And if MinIO had?

## Check your predictions

**P1.** Ten rows, in `events_core` (and `events_full`):

```
events_full: 10    events_core: 10    observations: 0    traces: 0
AGENT │ invoke_agent lab-agent │ 5
TOOL  │ echo                   │ 5
```

Five traces of two spans: the sixth was the one sent with the bad key in Lab 02, dropped by the Collector and
never stored. Langfuse v4 writes each span as one row in an *events* table; the older `traces` and `observations`
tables stay empty in this "events-only" mode (which is also why some older API endpoints return 404, see
[docs/09](../docs/09-online-evals-and-monitoring.md)).

**P2.** No. Postgres holds the project (`ec2-agent|EC2 Inventory Agent`), the public key (`pk-lf-3a85459f...`, not
secret), and only a **bcrypt hash** of the secret key (`$2a$11$...`). The real key lives in OpenBao. There are
`0 datasets` until Lab 07 creates one.

**P3.** One object per ingested OTLP request: 5, with keys like
`events/otel/ec2-agent/2026/10/11/12/26/<uuid>.json`, partitioned by project and minute. Redis has a few keys per
BullMQ queue (`bull:otel-ingestion-queue`, `bull:event-propagation-queue`, `bull:data-retention-queue`, ...).

**P4.** Everything up to the queue moved, nothing after it: the Collector's `exporter_sent_spans` went up (10), so
it got a 2xx; MinIO went from 5 to 6 objects; the queue had `wait=1`; ClickHouse stayed at 10 rows; `lf_trace`
returned 0 lines. After starting the worker, the queue drained to 0, ClickHouse went to 12 rows, and the trace
appeared. "Sent successfully" only means *accepted for processing*.

**P5.** Redis wiped: the queued job is gone, but the raw event is still in MinIO, so it can be replayed. That's why
the event goes to S3 *before* the queue: S3 is the durable log, Redis is only a work list. MinIO wiped: events not
yet processed are lost, and you lose the ability to rebuild ClickHouse from raw events. In production that bucket
is real S3 with versioning and a lifecycle rule.

## What to remember

- web accepts and stores the raw event in S3, Redis queues it, the worker writes ClickHouse. Postgres holds
  metadata, not traces.
- A 2xx from the ingestion API is not "stored". If traces are missing but the Collector shows them sent, check the
  queue and the worker.
- The same architecture you know from batch: a durable raw log, a queue to decouple, a columnar store for queries.

Next: [Lab 05 · An agent run as spans](05-spans.md).
