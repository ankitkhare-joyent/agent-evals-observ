# 10 · Secrets with OpenBao

## Why OpenBao and not Vault

HashiCorp Vault was the standard secrets manager for a decade under the MPL-2.0 open source licence. In
August 2023 HashiCorp moved Vault (and Terraform) to the **Business Source License 1.1**: you can read the
code and use it internally, but you may not offer it in a product that competes with HashiCorp's paid
offerings, and it is no longer OSI open source. IBM completed its acquisition of HashiCorp in 2025.

**OpenBao** is the community fork of Vault 1.14, the last MPL release, governed under the Linux
Foundation, MPL-2.0. It is the Vault equivalent of what OpenTofu is to Terraform. Practically:

- Same HTTP API, same concepts, same auth methods and secrets engines. The `bao` CLI mirrors `vault`.
  Vault clients (Python `hvac`, `hashicorp/vault-action`, Vault Agent templates, the External Secrets
  Operator's Vault provider) work against OpenBao unchanged. We use `hvac` here.
- It has diverged in useful ways: **namespaces** in the open source edition (an Enterprise feature in
  Vault), the **static-key auto-unseal**, **declarative self-initialization** and declarative audit
  devices (all used here), horizontal read scaling since 2.5, and ongoing releases (2.7.1 in October 2026).
- Vault Enterprise still has more enterprise-only features (DR replication, Sentinel, etc.). If you need
  those and are happy with BSL plus a contract, Vault is fine; for an open source platform, OpenBao.

## The mental model

```
                          ┌───────────────── OpenBao ─────────────────┐
  who are you?  ──login──►│ auth methods   userpass/OIDC (humans)      │
                          │                AppRole, Kubernetes, AWS,   │──► token (TTL, policies)
                          │                JWT/OIDC (CI)               │
  what may you do? ──────►│ policies       path "secret/data/x" {read} │
  give me a secret ──────►│ secrets engines                            │
                          │   kv-v2        static, versioned           │
                          │   database     dynamic users with leases   │
                          │   pki, transit, aws, ssh ...               │
                          │ audit log      every request, HMAC'd       │
                          │ storage        Raft (integrated), encrypted│
                          │ seal           root key wrapped by KMS/key │
                          └────────────────────────────────────────────┘
```

- **Everything is a path**, and a **policy** is a list of paths with capabilities. Default deny.
- **Auth methods** turn an identity into a **token** that carries policies and a TTL. Tokens expire;
  long-running clients renew them ([observ/secrets.py](../observ/secrets.py) `TokenKeeper`, or the Agent).
- **Secrets engines** are mounted at paths. KV v2 stores static secrets with versions and check-and-set;
  the `database` engine *creates* credentials on demand and destroys them when their **lease** ends.
- **Seal / unseal**: data on disk is encrypted with a key that is itself encrypted by the *root key*.
  On start the server is *sealed* until the root key is available: Shamir key shares typed by humans,
  or **auto-unseal** through a KMS or (OpenBao) a static key.
- **Audit devices** log every request and response, with secret values HMAC'd.

## How this project uses it

| Piece | File | What it does |
|---|---|---|
| Server config | [stack/openbao/config.hcl](../stack/openbao/config.hcl) | Raft storage, static-key auto-unseal, stdout audit device, and **self-initialization**: on first boot it mounts KV v2, enables AppRole and userpass, loads 4 policies, creates the admin user and 3 AppRoles. The root token it used is revoked when it's done. |
| Bootstrap | [scripts/bootstrap.sh](../scripts/bootstrap.sh) | Generates the 32-byte unseal key and the first admin password into `.bao/` (gitignored). |
| Policies | [stack/openbao/policies/](../stack/openbao/policies/) | `langfuse-infra` reads one path, `otel-collector` one path, `eval-runner` two paths, `admin` manages but isn't root. |
| Seeding | [scripts/bao_seed.py](../scripts/bao_seed.py) | As admin: generates Langfuse's DB/cache/S3 passwords and keys once (KV check-and-set `cas=0`, so re-runs never rotate a live DB password), stores the Anthropic key from `.env`, issues each AppRole a secret_id. |
| Sidecars | [stack/openbao/agent/](../stack/openbao/agent/) | Two **OpenBao Agent** containers, each with its own AppRole, render secrets into tmpfs volumes: Langfuse's env file, Postgres/ClickHouse/MinIO password files, Redis config; and the Collector's `Authorization` header. They renew their tokens and re-render on change. |
| Consumers | [compose.yaml](../compose.yaml) | Postgres `POSTGRES_PASSWORD_FILE`, ClickHouse `CLICKHOUSE_PASSWORD_FILE`, Redis config file, Langfuse sources the env file before its entrypoint, the Collector reads `${file:...}`. No secret appears in compose or git. |
| In-process | [observ/secrets.py](../observ/secrets.py) | The eval runner logs in with AppRole, reads two secrets, puts them in its own environment, revokes its token. |
| Dynamic secrets | [scripts/demo_dynamic_db_creds.py](../scripts/demo_dynamic_db_creds.py) | Creates a 1-hour read-only Postgres user on demand, uses it, revokes the lease, shows the user is gone. |

Each system owns its secrets: the agent's own API login stays in the lakehouse project's OpenBao; this
project never copies it.

## The "secret zero" problem

Every scheme has a first secret that gets you the others. Here it is the AppRole `secret_id` in
`.bao/approle/<role>/`. Locally that's acceptable. In production you remove it:

- **Kubernetes**: the `kubernetes` auth method. The pod's service-account JWT *is* its identity; the
  OpenBao Agent Injector (or the CSI provider, or External Secrets Operator) logs in with it and writes
  secrets into an in-memory volume. That's exactly our sidecar pattern, generated for you by annotations.
- **AWS (EC2/EKS/Lambda)**: the `aws` auth method verifies a signed STS `GetCallerIdentity` request, so
  the IAM role is the identity.
- **CI**: `jwt` auth with GitHub/GitLab OIDC tokens (see [.github/workflows/evals.yml](../.github/workflows/evals.yml)).
- **Humans**: OIDC through your IdP with MFA. `userpass` is only break-glass.
- Where AppRole remains: deliver secret_ids **response-wrapped** (single-use, short TTL), with
  `secret_id_num_uses=1` and CIDR binding.

## Production checklist

- 3 or 5 Raft nodes across zones, TLS on every listener, a load balancer on the active node's health.
- KMS auto-unseal (`awskms`, `gcpckms`, `azurekeyvault`, `pkcs11`); recovery keys split among people.
- No root token in existence; self-init or generate-root only for emergencies.
- Audit devices to a SIEM, with an alert on audit failure (OpenBao blocks requests if no audit device can write).
- Short token TTLs, renewal by agents, least-privilege policies reviewed like code (they *are* code here).
- Prefer dynamic secrets (database, AWS, PKI) over static; rotate static ones (KV versions make it safe).
- Snapshots of Raft (`bao operator raft snapshot save`) on a schedule, restore tested.

## Try it

```bash
open http://localhost:38200/ui          # log in: method userpass, user admin, password in .bao/bootstrap/admin-password
docker compose logs openbao | grep '"type":"request"' | tail -3     # the audit log
mise run demo-dynamic-creds                                          # dynamic Postgres credentials
docker compose exec bao-agent-collector cat /secrets/langfuse_otlp_authorization | cut -c1-12   # rendered, in tmpfs
```

Rotate something and watch it flow: write a new `langfuse/otlp` key version in the UI, and within
5 minutes (`static_secret_render_interval`) the sidecar re-renders the file. The Collector reads it at
start, so restart it (`docker compose restart otel-collector`); in Kubernetes the Agent can signal or
restart the process for you.

Next: [11 · Production checklist](11-production-checklist.md)
