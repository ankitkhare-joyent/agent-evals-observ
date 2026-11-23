# Lab 01 · OpenBao: where every credential comes from

One sitting, about 30 minutes. Background: [docs/10](../docs/10-openbao-secrets.md). Config and code:
[stack/openbao/config.hcl](../stack/openbao/config.hcl), [stack/openbao/policies/](../stack/openbao/policies),
[scripts/bao_seed.py](../scripts/bao_seed.py), [observ/secrets.py](../observ/secrets.py).

OpenBao comes first because nothing else in this stack starts without it: Postgres, ClickHouse, Redis, MinIO,
Langfuse and the Collector all get their passwords and keys from it. After this lab you can explain:

- how a server can initialize itself without anyone ever holding a root token
- why it comes back unsealed after a restart, and what replaces that in production
- what each workload is allowed to read, and how to find out without reading code
- why a "healthy" container can be failing to authenticate

Answers to every **Predict** are at the bottom. Write your guess down before you run the command.

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
docker compose ps --format '{{.Service}}\t{{.Status}}'     # 11 services, all Up
```

## 1. Is the vault open, and how?

**Predict (P1):** you never typed an unseal key. Is OpenBao sealed? What seal type is it using, and how many
unseal key shares (`t` of `n`) exist?

```bash
curl -s $EVALS_BAO_ADDR/v1/sys/health | jq -c '{initialized, sealed, version}'
curl -s $EVALS_BAO_ADDR/v1/sys/seal-status | jq -c '{type, t, n, storage_type, recovery_seal}'
```

## 2. Who initialized it?

Self-initialization ran the `initialize` blocks in [config.hcl](../stack/openbao/config.hcl) on the first start.

**Predict (P2):** the requests in those blocks need a root token. Where is it now?

```bash
docker compose logs --no-log-prefix openbao | grep -E 'post-unseal configuration|successful mount|enabled credential|revoked lease.*root' | cut -c1-140
```

## 3. Who am I, and what is in here?

`lab/env.sh` logged you in as the human `admin` (userpass) with the password `scripts/bootstrap.sh` generated.

**Predict (P3):** which policies does your token carry, and how long does it live?

```bash
curl -s -H "X-Vault-Token: $EVALS_BAO_TOKEN" $EVALS_BAO_ADDR/v1/auth/token/lookup-self \
  | jq -c '.data | {display_name, policies, ttl_min: (.ttl/60|floor), renewable}'
curl -s -X LIST -H "X-Vault-Token: $EVALS_BAO_TOKEN" $EVALS_BAO_ADDR/v1/secret/metadata/langfuse | jq -c .data.keys
for s in langfuse/infra langfuse/otlp langfuse/api evals/anthropic; do printf '%-16s' $s; bao_keys $s; done
```

## 4. Least privilege, seen from outside

Now log in as each workload with its own AppRole (`role_id` + `secret_id` from `.bao/approle/<role>/`) and try
the same four reads.

**Predict (P4):** fill in a 3 × 4 grid: which of the four secrets can `eval-runner`, `otel-collector` and
`langfuse-infra` each read?

```bash
for role in eval-runner otel-collector langfuse-infra; do
  T=$(approle_token $role); echo "== $role"
  for s in langfuse/infra langfuse/otlp langfuse/api evals/anthropic; do printf '  %-16s' $s; TOKEN=$T bao_keys $s; done
done
```

You found that out by trying. The polite way is to ask the server what a token may do:

```bash
T=$(approle_token eval-runner)
curl -s -H "X-Vault-Token: $T" -X POST $EVALS_BAO_ADDR/v1/sys/capabilities-self \
  -d '{"paths":["secret/data/evals/anthropic","secret/data/langfuse/infra","sys/policies/acl/eval-runner"]}' | jq -c .
curl -s -H "X-Vault-Token: $T" $EVALS_BAO_ADDR/v1/auth/token/lookup-self | jq -c '.data | {display_name, policies, ttl_min: (.ttl/60|floor)}'
```

## 5. The audit log saw all of that

**Predict (P5):** OpenBao logged every request you just made. Will the log line for the successful read of
`evals/anthropic` contain your Anthropic key?

```bash
docker compose logs --no-log-prefix openbao | grep '^{' | jq -c 'select(.type=="response"
  and .request.path=="secret/data/evals/anthropic" and (.auth.policies|index("eval-runner")))
  | {path: .request.path, who: .auth.display_name, policies: .auth.policies, api_key: .response.data.data.api_key}' | tail -1
docker compose logs --no-log-prefix openbao | grep '^{' | jq -c 'select(.type=="response"
  and .request.path=="secret/data/langfuse/infra" and .error!=null) | {path: .request.path, who: .auth.display_name, error}' | tail -1
```

## 6. Restart it

**Predict (P6):** after `docker compose restart openbao`, will it come back sealed?

```bash
docker compose restart openbao && sleep 3
curl -s $EVALS_BAO_ADDR/v1/sys/health | jq -c '{initialized, sealed}'
docker compose logs --no-log-prefix --since 30s openbao | grep -iE 'unseal' | cut -c1-110
```

## Break it, then fix it

**Break: a sidecar loses its credential.** The Collector's sidecar logs in with the `secret_id` in
`.bao/approle/otel-collector/`. Delete it and restart the sidecar (this lab *changes* something: the fix issues a
new secret_id):

```bash
rm .bao/approle/otel-collector/secret_id
docker compose restart bao-agent-collector && sleep 6
docker compose ps --format '{{.Service}}\t{{.Status}}' bao-agent-collector otel-collector
docker compose logs --no-log-prefix --since 15s bao-agent-collector | grep -iE 'error' | head -3 | cut -c1-160
curl -s localhost:34133 | jq -c .          # the Collector's health
```

**Predict (P7):** what does `docker compose ps` say about the sidecar, and is the Collector still able to ship
traces to Langfuse right now? What would make that stop?

**Fix:** `bao_seed.py` issues a secret_id for any role that is missing one. Then restart the sidecar:

```bash
mise exec -- uv run python -m scripts.bao_seed | tail -3
docker compose restart bao-agent-collector && sleep 5
docker compose logs --no-log-prefix --since 8s bao-agent-collector | grep -E 'authentication successful'
```

## Check your predictions

**P1.** `{"initialized":true,"sealed":false,"version":"2.7.1"}` and
`{"type":"static","t":0,"n":0,"storage_type":"raft","recovery_seal":true}`. No unseal key shares exist at all
(`t` and `n` are 0): the static auto-unseal key in `.bao/bootstrap/unseal.key` unwraps the root key at every start.
`recovery_seal: true` means the auto-unseal setup uses *recovery* keys for emergencies instead of unseal keys,
and self-initialization doesn't generate even those. In production the static key is replaced by a KMS
(`awskms`, `gcpckms`, ...), so the key that protects everything never sits on the same disk as the data.

**P2.** Revoked, a second after it was used:

```
beginning post-unseal configuration
core: successful mount: namespace="" path=secret/ type=kv version=""
core: enabled credential backend: namespace="" path=approle/ type=approle version=""
core: enabled credential backend: namespace="" path=userpass/ type=userpass version=""
expiration: revoked lease: lease_id=auth/token/root/h540b95c...
```

The mounts, policies, admin user and AppRoles were all created with a root token that existed only inside the
server process and was revoked at the end. No one has ever seen it, so there is none to leak.

**P3.** `{"display_name":"userpass-admin","policies":["admin","default"],"ttl_min":59,"renewable":true}`: one
hour, renewable up to the 8-hour max set on the user. The listing shows `["api","infra","otlp"]` under
`langfuse/`, and admin can read every secret's field names:

```
langfuse/infra  {"fields":["clickhouse_password","encryption_key","init_user_password","minio_password","nextauth_secret","postgres_password","project_public_key","project_secret_key","redis_password","salt"],"version":1}
langfuse/otlp   {"fields":["host","public_key","secret_key"],"version":1}
langfuse/api    {"fields":["host","public_key","secret_key"],"version":1}
evals/anthropic {"fields":["api_key"],"version":1}
```

**P4.** Each workload reads exactly what its one policy lists, and everything else is `permission denied`:

| | langfuse/infra | langfuse/otlp | langfuse/api | evals/anthropic |
|---|---|---|---|---|
| eval-runner | denied | denied | ✓ | ✓ |
| otel-collector | denied | ✓ | denied | denied |
| langfuse-infra | ✓ | denied | denied | denied |

`langfuse/otlp` and `langfuse/api` hold the same key but live at two paths so two policies can grant them
separately. `capabilities-self` answers without trying:
`{"secret/data/evals/anthropic":["read"],"secret/data/langfuse/infra":["deny"],"sys/policies/acl/eval-runner":["deny"]}`,
and the eval-runner token is `{"display_name":"approle","policies":["default","eval-runner"],"ttl_min":30}`: half
an hour, which is why long eval runs need renewal (`TokenKeeper` in [observ/secrets.py](../observ/secrets.py)).

**P5.** No. Secret values in the audit log are HMAC'd:

```
{"path":"secret/data/evals/anthropic","who":"approle","policies":["default","eval-runner"],"api_key":"hmac-sha256:442e7704c36f..."}
{"path":"secret/data/langfuse/infra","who":"approle","error":"1 error occurred:\n\t* permission denied\n\n"}
```

The HMAC is still useful: hash a suspected leaked key with the audit device's HMAC key
(`sys/audit-hash/stdout`) and grep for it to see who read it and when. Denied attempts are logged too, with the
identity and policies, which is exactly what a security team alerts on.

**P6.** Not sealed: `{"initialized":true,"sealed":false}`. The log shows it seal on shutdown and then
`stored unseal keys supported, attempting fetch` → `vault is unsealed` → `unsealed with stored key` on start.
With Shamir unseal (the learn-ai-agent stack) a restart leaves it sealed until someone runs the unseal step.

**P7.** `bao-agent-collector  Up 6 seconds (healthy)`, while its log says
`agent.auth.handler: error getting path or data from method: error="no known secret ID" backoff=1s`, retrying with
a growing backoff. The health check only tests that the rendered file exists, and it does: the file lives in a tmpfs
volume the Collector still has mounted, so the Collector keeps shipping with the key rendered earlier
(`{"status":"Server available",...}`). It stops when that file disappears (both containers restarted, so the tmpfs
is recreated empty) or when the key rotates and the sidecar can't fetch the new one. Lesson: a health check that
tests the *output* can stay green while the *process* is broken. A better one would check the agent's token
(its `/agent/v1/metrics` or a token-age check).

The fix prints `issued AppRole secret_id for otel-collector` and the sidecar logs
`authentication successful, sending token to sinks`. Note `stored secret/evals/anthropic from ./.env`:
`bao_seed.py` writes the Anthropic key on every run, so that secret's version goes up by one each time.

## What to remember

- Self-init + auto-unseal means no human ever holds a root token or an unseal key, and restarts need no one.
- One AppRole and one policy per workload. You can list a path you can't read, and `capabilities-self` tells you
  what a token may do.
- The audit log has every request, allowed or denied, with values HMAC'd.
- "Healthy" only means what the health check tests.

Next: [Lab 02 · Secret delivery](02-secret-delivery.md).
