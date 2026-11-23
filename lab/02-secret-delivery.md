# Lab 02 · Secret delivery: from OpenBao into a running process

One sitting, about 30 minutes. Background: [docs/10](../docs/10-openbao-secrets.md) ("How this project uses it").
Config: [compose.yaml](../compose.yaml), [stack/openbao/agent/](../stack/openbao/agent),
[stack/openbao/templates/](../stack/openbao/templates).

Lab 01 showed what each workload may read. This one follows a secret the rest of the way: the OpenBao Agent
sidecar renders it into a memory-only volume, and each service picks it up in its own way. After this lab you can
explain:

- why `compose.yaml` and `docker inspect` contain no passwords, and where the real process gets them
- the four ways services here consume a rendered secret
- what rotating a secret actually takes, and how KV versions let you roll back a bad one

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
```

## 1. Search the config for secrets

**Predict (P1):** `docker compose config` prints the fully resolved configuration. Will any password or key
*value* appear in it?

```bash
docker compose config | grep -iE 'PASSWORD|SECRET|_KEY' | sed 's/^ *//' | sort -u
```

## 2. Where the files are

**Predict (P2):** the Postgres container reads `/secrets/postgres_password`. What kind of filesystem is
`/secrets`, who owns the files, and can Postgres write to them?

```bash
docker compose exec postgres sh -c 'mount | grep /secrets; ls -ln /secrets'
docker compose logs --no-log-prefix bao-agent-langfuse | grep -E 'rendered|authentication successful' | cut -c26-140 | head -7
```

## 3. Which process has them

Langfuse can't read a password file, so its entrypoint sources `/secrets/langfuse.env` and then `exec`s the server.

**Predict (P3):** `docker inspect` shows the container's configured environment. Will it list `DATABASE_URL`
or `SALT`? Will the running server process have them? Will PID 1?

```bash
docker inspect agent-observ-langfuse-web-1 --format '{{json .Config.Env}}' | jq -r '.[]' | cut -d= -f1 \
  | grep -E 'DATABASE_URL|SALT|NEXTAUTH_SECRET|ENCRYPTION_KEY|CLICKHOUSE_PASSWORD'; echo "(inspect done)"
docker compose exec langfuse-web sh -c 'p=$(pgrep -o -f next-server); echo "pid $p: $(tr "\0" " " < /proc/$p/cmdline)"
  tr "\0" "\n" < /proc/$p/environ | cut -d= -f1 | grep -E "^(DATABASE_URL|SALT|NEXTAUTH_SECRET|ENCRYPTION_KEY|CLICKHOUSE_PASSWORD|LANGFUSE_INIT_PROJECT_SECRET_KEY)$" | sort | tr "\n" " "; echo
  echo "pid 1 has: $(tr "\0" "\n" < /proc/1/environ | cut -d= -f1 | grep -cE "^(DATABASE_URL|SALT|NEXTAUTH_SECRET)$") of them"'
```

**Predict (P4):** list the four different ways the services in [compose.yaml](../compose.yaml) consume a
rendered secret. (Look at postgres, clickhouse, redis, minio, langfuse-web and otel-collector.)

## 4. A baseline trace through the Collector

Lab 03 covers the Collector properly. For now, send one hand-made span and check it reaches Langfuse with the
key the sidecar rendered:

```bash
TID=$(mise run -q lab:span); echo $TID; sleep 20
lf_trace $TID
otel_stats | grep -E 'exporter_(sent|send_failed)'
```

## Break it, then fix it

**Break: rotate in a bad key.** Write a new version of `langfuse/otlp` whose secret key is wrong (this lab
*changes* something: it writes two new versions of that secret). The public key is copied in without printing it:

```bash
jq -n --arg pk "$(bao_field langfuse/otlp public_key)" \
  '{data: {public_key: $pk, secret_key: "sk-lf-00000000-0000-0000-0000-000000000000", host: "http://localhost:33000"}}' \
  | curl -s -H "X-Vault-Token: $EVALS_BAO_TOKEN" -X POST -d @- $EVALS_BAO_ADDR/v1/secret/data/langfuse/otlp | jq -c '.data | {version}'
docker compose exec bao-agent-collector sh -c 'stat -c "%y" /secrets/langfuse_otlp_authorization'
```

**Predict (P5):** the secret is now version 2. Has the Collector's header file changed yet? When would it change
on its own, and when would the Collector start using it?

Make it take effect now, then send a span:

```bash
docker compose restart bao-agent-collector && sleep 4
docker compose exec bao-agent-collector sh -c 'stat -c "%y" /secrets/langfuse_otlp_authorization'
docker compose restart otel-collector && sleep 3
TID=$(mise run -q lab:span); sleep 20
lf_trace $TID                                                   # anything?
docker compose logs --no-log-prefix --since 1m otel-collector | grep 'Exporting failed' | grep -oE '"error": "[^"]{0,200}'
otel_stats | grep -E 'exporter_(sent|send_failed)'
```

**Predict (P6):** what does the app that produced the span see? What is lost, and where would you have noticed?

**Fix: roll back.** KV v2 keeps old versions. "Rollback" means writing version 1's data as a new version
(this is what `bao kv rollback` does). Nothing is printed on the way through:

```bash
curl -s -H "X-Vault-Token: $EVALS_BAO_TOKEN" $EVALS_BAO_ADDR/v1/secret/metadata/langfuse/otlp | jq -c '{versions: (.data.versions | keys)}'
curl -s -H "X-Vault-Token: $EVALS_BAO_TOKEN" "$EVALS_BAO_ADDR/v1/secret/data/langfuse/otlp?version=1" | jq '{data: .data.data}' \
  | curl -s -H "X-Vault-Token: $EVALS_BAO_TOKEN" -X POST -d @- $EVALS_BAO_ADDR/v1/secret/data/langfuse/otlp | jq -c '.data | {version}'
docker compose restart bao-agent-collector && sleep 4 && docker compose restart otel-collector && sleep 3
TID=$(mise run -q lab:span); sleep 20; lf_trace $TID
```

## Check your predictions

**P1.** No values, only *references* to where secrets will be: `POSTGRES_PASSWORD_FILE: /secrets/postgres_password`,
`CLICKHOUSE_PASSWORD_FILE: /secrets/clickhouse_password`, `/secrets/redis.conf`, `/secrets/minio_password`, the
`. /secrets/langfuse.env` entrypoint lines, and the two tmpfs volumes. The `ACCESS_KEY_ID: minio` lines are a
username, not a secret. You could commit this file to a public repo and leak nothing.

**P2.** `tmpfs on /secrets type tmpfs (ro,relatime,size=1024k,mode=755,uid=100,gid=1000)`: memory only, 1 MB,
owned by uid 100 (the `openbao` user the sidecar runs as), mounted **read-only** into Postgres. The files are
`-r--r--r-- 100 1000`: `clickhouse_password`, `langfuse.env` (1014 bytes), `minio_password`, `postgres_password`,
`redis.conf`. Nothing ever touches a disk, so nothing ends up in a volume backup or an image layer. The sidecar's
log shows `authentication successful`, then one `rendered ... => /secrets/...` line per file.

**P3.** `docker inspect` lists none of them (`(inspect done)` with nothing above it). The server process has all of
them: `pid 7: next-server (v16.3.8)` with `CLICKHOUSE_PASSWORD DATABASE_URL ENCRYPTION_KEY
LANGFUSE_INIT_PROJECT_SECRET_KEY NEXTAUTH_SECRET SALT`. PID 1 has `0 of them`: here PID 1 is `dumb-init`, which
started `sh`; the shell sourced the file and `exec`'d the entrypoint, so only that process tree carries the values.
Note the process renamed itself to `next-server`, which is why `pgrep -f node` finds nothing.

**P4.** Four patterns, from best to most fiddly:
1. **Native `_FILE` variables**: Postgres (`POSTGRES_PASSWORD_FILE`) and ClickHouse (`CLICKHOUSE_PASSWORD_FILE`)
   read the password from a file themselves.
2. **A rendered config file**: Redis runs `redis-server /secrets/redis.conf`, so the password is never on a command
   line where `ps` would show it.
3. **A wrapper that reads the file and execs**: MinIO (`export MINIO_ROOT_PASSWORD="$(cat ...)"`) and Langfuse
   (`set -a; . /secrets/langfuse.env`).
4. **Config-file indirection in the app**: the Collector's `${file:/secrets/langfuse_otlp_authorization}`.

**P4 baseline (step 4).** The trace has `AGENT invoke_agent lab-agent` and `TOOL echo`, and
`otelcol_exporter_sent_spans{exporter="otlphttp/langfuse",...} 2`.

**P5.** No: the file still has its original time (`12:23:08` in my run) right after the write. The sidecar re-reads
KV secrets every 5 minutes (`static_secret_render_interval`), and the Collector only reads the file at startup, so
on its own the bad key would arrive within 5 minutes and be used at the Collector's next restart. A delayed
failure that happens at the next deploy is the worst kind. After restarting the sidecar the file time changes
(`12:27:16`).

**P6.** Nothing: `mise run lab:span` printed a trace ID and exited 0, and an agent would have answered normally.
`lf_trace` prints nothing, and the Collector logs `Exporting failed. Dropping data.` with
`Permanent error: ... responded with HTTP Status Code 401`. A 401 isn't retried, so these spans are **lost**.
`otel_stats` shows `otelcol_exporter_send_failed_spans{exporter="otlphttp/langfuse",...} 2` and
`otelcol_exporter_sent_spans ... 0`. That counter is what you'd alert on (Prometheus scrapes it as the
`otel-collector-self` job). Telemetry failures are deliberately invisible to the app.

**Fix.** The metadata shows `{"versions":["1","2"]}`, the rollback writes `{"version":3}` (with version 1's
data), and after the two restarts the new span arrives: `AGENT invoke_agent lab-agent` and `TOOL echo`. Version 2
still exists for the audit trail. In production you would rotate by adding the new key in Langfuse first, rolling it
out, and only then revoking the old one, so there is never a moment when the deployed key is invalid.

## What to remember

- Config says *where* secrets are, never *what* they are. Values live in tmpfs and in the memory of the one
  process that needs them.
- Prefer `_FILE` support or a config file; fall back to a wrapper that reads and `exec`s.
- A rotated secret reaches a service only when both the sidecar has re-rendered it and the service has re-read it.
- Telemetry export failures don't reach the app. Watch the Collector's own `send_failed` counter.

Next: [Lab 03 · The Collector](03-collector.md).
