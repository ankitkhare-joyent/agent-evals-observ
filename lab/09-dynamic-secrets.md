# Lab 09 · Dynamic secrets: a database user that exists for an hour

One sitting, about 30 minutes. Background: [docs/10](../docs/10-openbao-secrets.md) (secrets engines, leases).
Code: [scripts/demo_dynamic_db_creds.py](../scripts/demo_dynamic_db_creds.py).

Every secret so far was *static*: generated once, stored, rotated by hand. The database secrets engine is
different: no credential exists until someone asks, and each one disappears on its own. This lab **changes
something**: it creates and drops temporary Postgres users in Langfuse's database and adds a role to OpenBao.
After it you can explain:

- what OpenBao does in Postgres when you read `database/creds/<role>`
- leases: TTL, renewal, max TTL, expiry, revocation
- how you'd answer "this credential leaked" in one API call

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
H="X-Vault-Token: $EVALS_BAO_TOKEN"
```

## 1. The guided tour

```bash
mise run -q demo-dynamic-creds
```

Read [demo_dynamic_db_creds.py](../scripts/demo_dynamic_db_creds.py) alongside the output: it enables the engine,
configures a connection to Langfuse's Postgres, defines the `analyst-readonly` role, issues one credential, uses
it, and revokes it.

**Predict (P1):** the script printed `can read: 0 prompts`. Why can this brand-new user read a table, but not create one?

## 2. By hand, with the API

Issue a credential into shell variables (nothing printed but the username), then look at it from both sides:

```bash
curl -s -H "$H" $EVALS_BAO_ADDR/v1/database/roles/analyst-readonly | jq -c '.data | {db_name, default_ttl, max_ttl, creation_statements}'
C=$(curl -s -H "$H" $EVALS_BAO_ADDR/v1/database/creds/analyst-readonly)
U=$(jq -r .data.username <<<"$C"); P=$(jq -r .data.password <<<"$C"); L=$(jq -r .lease_id <<<"$C")
echo "user=$U lease_ttl=$(jq .lease_duration <<<"$C") renewable=$(jq .renewable <<<"$C")"
docker compose exec -T postgres psql -U postgres -tAc "select rolname, rolvaliduntil, rolsuper from pg_roles where rolname like 'v-%'"
docker compose exec -T -e PGPASSWORD="$P" postgres psql -h 127.0.0.1 -U "$U" -d postgres -tAc "select count(*) || ' projects' from projects"
```

**Predict (P2):** the username encodes something about who asked. What? And how does Postgres itself know the
password stops working in an hour, even if OpenBao were down?

Now renew the lease for two more hours:

```bash
curl -s -H "$H" -X PUT $EVALS_BAO_ADDR/v1/sys/leases/lookup -d "{\"lease_id\":\"$L\"}" | jq -c '.data | {ttl, expire_time, renewable}'
curl -s -H "$H" -X PUT $EVALS_BAO_ADDR/v1/sys/leases/renew -d "{\"lease_id\":\"$L\",\"increment\":7200}" | jq -c '{lease_duration}'
docker compose exec -T postgres psql -U postgres -tAc "select rolname, rolvaliduntil from pg_roles where rolname like 'v-%'"
```

**Predict (P3):** what happens in Postgres when the lease is renewed? What if you asked for 10 hours?

## Break it, then fix it

**Break: a role the connection doesn't allow.** Define a stricter role, valid for 20 seconds, and ask for a
credential:

```bash
curl -s -H "$H" -X POST $EVALS_BAO_ADDR/v1/database/roles/analyst-20s -d '{"db_name":"langfuse-postgres","default_ttl":"20s","max_ttl":"20s",
  "creation_statements":["CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '"'"'{{password}}'"'"' VALID UNTIL '"'"'{{expiration}}'"'"';","GRANT pg_read_all_data TO \"{{name}}\";"]}' \
  -o /dev/null -w '%{http_code}\n'
curl -s -H "$H" $EVALS_BAO_ADDR/v1/database/creds/analyst-20s | jq -c .
curl -s -H "$H" $EVALS_BAO_ADDR/v1/database/config/langfuse-postgres | jq -c '.data | {allowed_roles, username: .connection_details.username}'
```

**Predict (P4):** the role was created (204), so why can't it issue credentials? Who decides?

**Fix:** allow it on the connection, then watch a credential live and die:

```bash
curl -s -H "$H" -X POST $EVALS_BAO_ADDR/v1/database/config/langfuse-postgres -d '{"allowed_roles":["analyst-readonly","analyst-20s"]}' -o /dev/null -w '%{http_code}\n'
C=$(curl -s -H "$H" $EVALS_BAO_ADDR/v1/database/creds/analyst-20s); U2=$(jq -r .data.username <<<"$C"); P2=$(jq -r .data.password <<<"$C")
docker compose exec -T -e PGPASSWORD="$P2" postgres psql -h 127.0.0.1 -U "$U2" -d postgres -tAc "select 'works'"
sleep 25
docker compose exec -T -e PGPASSWORD="$P2" postgres psql -h 127.0.0.1 -U "$U2" -d postgres -tAc "select 'works'" 2>&1 | cut -c1-120
docker compose logs --no-log-prefix --since 2m openbao | grep 'revoked lease' | grep analyst-20s | cut -c1-120 | tail -1
```

**Incident drill: "the analyst credential leaked".** Revoke every credential ever issued from that role in one call:

```bash
docker compose exec -T postgres psql -U postgres -tAc "select count(*) || ' dynamic roles' from pg_roles where rolname like 'v-%'"
curl -s -H "$H" -X PUT $EVALS_BAO_ADDR/v1/sys/leases/revoke-prefix/database/creds/analyst-readonly -o /dev/null -w '%{http_code}\n'
docker compose exec -T postgres psql -U postgres -tAc "select count(*) || ' dynamic roles' from pg_roles where rolname like 'v-%'"
```

**Predict (P5):** what would the same drill take with a static, shared read-only password?

## Check your predictions

**P1.** The role's creation SQL grants `pg_read_all_data`, Postgres's built-in read-everything role, and nothing
else, so `select` works and `create table nope(x int)` fails with `permission denied for schema public`. The SQL is
yours to write: in production you'd grant read on specific schemas only. After `revoke`, the same login fails with
`FATAL: role "v-userpass-analyst--…" does not exist`: OpenBao ran `DROP ROLE`.

**P2.** `user=v-userpass-analyst--kFkA2H1Vd0jZlZDxc4BP-1791723297 lease_ttl=3600 renewable=true`. The name is
`v-<auth method>-<role prefix>-<random>-<unix time>`: issued to someone who logged in with **userpass**, for the
**analyst** role, at that time. A DBA looking at `pg_stat_activity` can tell where a session came from. Postgres knows
the expiry because the role was created `VALID UNTIL '{{expiration}}'`: `rolvaliduntil` was `13:55:02+00`, one
hour out. Even if OpenBao were down and couldn't run its `DROP ROLE`, the password stops working at that time.
`1 projects`: the new user can read Langfuse's tables.

**P3.** The lookup showed `{"ttl":3598, ..., "renewable":true}` and the renewal returned `{"lease_duration":7200}`.
In Postgres, `rolvaliduntil` moved from `13:55` to `14:55`: OpenBao runs `ALTER ROLE … VALID UNTIL` on renewal, so
both sides agree. Asking for 10 hours gets you capped at the role's `max_ttl` (4 hours from issue); after that the
only way forward is a new credential, so no credential lives forever.

**P4.** `"analyst-20s" is not an allowed role`. A *role* is just a template; the *connection* config has an
`allowed_roles` list (here `["analyst-readonly"]`, set by the demo script), and the connection holds the
privileged `postgres` login. Two separate decisions, which matters when different teams own the database
connection and the role definitions. After allowing it: `select 'works'` succeeds, and 20 seconds later the login
fails with `role "v-userpass-analyst--bQd6…" does not exist`. OpenBao's log shows
`expiration: revoked lease: lease_id=database/creds/analyst-20s/…`, nobody had to do anything.

**P5.** With dynamic credentials: `1 dynamic roles`, one `revoke-prefix` call (`204`), `0 dynamic roles`, and
every leaked copy is dead, while every *other* application keeps its own unaffected credentials. With a shared static
password you'd have to rotate it in the database, then update every consumer before their next connection, and
you still couldn't tell from the database logs who used the leaked one.

## What to remember

- A dynamic credential is created on read, carries a lease, and is dropped when the lease ends or is revoked.
- Expiry is enforced twice: by OpenBao's lease, and by the database (`VALID UNTIL`).
- Connection config (privileged, `allowed_roles`) and role definitions (what SQL runs) are separate controls.
- Leaks are a one-call revoke, scoped to a role, without touching anyone else.

That's the last lab. The diagram at the end of [README.md](README.md) shows where each one sits; the full system
is in [docs/12-architecture.md](../docs/12-architecture.md).
