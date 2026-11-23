# OpenBao server configuration. docs/10-openbao-secrets.md walks through every block.
#
# Production differences are called out inline; the shape is the same one you would run on Kubernetes
# (3 or 5 replicas of this file behind the OpenBao Helm chart).

ui            = true
disable_mlock = true          # containers can't mlock without IPC_LOCK; on VMs leave mlock on
api_addr      = "http://openbao:8200"
cluster_addr  = "http://openbao:8201"
log_level     = "info"

# Integrated Raft storage: no external Consul/etcd. Production runs 3 or 5 nodes for quorum.
storage "raft" {
  path    = "/openbao/data"
  node_id = "openbao-0"
}

# Production: TLS everywhere (tls_cert_file / tls_key_file). Plain HTTP is acceptable here only because
# the listener is bound to the Docker network and published on 127.0.0.1.
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

# Auto-unseal with a static key (OpenBao-only feature). The barrier's root key is encrypted with this key,
# so the server unseals itself on every start - no humans typing unseal shares after a restart.
# Production: a KMS seal instead (seal "awskms" / "gcpckms" / "azurekeyvault" / "pkcs11" / "transit"),
# so the key that protects every secret never sits on the same disk as the data.
seal "static" {
  current_key_id = "local-1"
  current_key    = "file:///openbao/bootstrap/unseal.key"
}

# Declarative audit device: every request and response is logged (secrets HMAC'd, never in clear text).
audit "file" "stdout" {
  description = "Audit log to container stdout (ship it to your SIEM in production)"
  options {
    file_path = "stdout"
  }
}

# ---------------------------------------------------------------------------------------------------------
# Declarative self-initialization (OpenBao 2.4+). Runs once, on the first start against empty storage.
# The root token it uses is revoked when it finishes, so no root token ever exists outside this process.
# Afterwards the only way in is the "admin" userpass login (for humans) or AppRole (for workloads).
# ---------------------------------------------------------------------------------------------------------
initialize "mounts" {
  request "kv" {
    operation = "update"
    path      = "sys/mounts/secret"
    data = {
      type        = "kv"
      description = "Static secrets (KV version 2: versioned, soft-delete, check-and-set)"
      options     = { version = "2" }
    }
  }
  request "approle" {
    operation = "update"
    path      = "sys/auth/approle"
    data      = { type = "approle", description = "Machine identities (sidecars, CI, eval runners)" }
  }
  request "userpass" {
    operation = "update"
    path      = "sys/auth/userpass"
    data      = { type = "userpass", description = "Break-glass human admin. Production: OIDC via your IdP" }
  }
}

initialize "policies" {
  request "admin" {
    operation = "update"
    path      = "sys/policies/acl/admin"
    data      = { policy = { eval_type = "string", eval_source = "file", path = "/openbao/policies/admin.hcl" } }
  }
  request "langfuse-infra" {
    operation = "update"
    path      = "sys/policies/acl/langfuse-infra"
    data      = { policy = { eval_type = "string", eval_source = "file", path = "/openbao/policies/langfuse-infra.hcl" } }
  }
  request "otel-collector" {
    operation = "update"
    path      = "sys/policies/acl/otel-collector"
    data      = { policy = { eval_type = "string", eval_source = "file", path = "/openbao/policies/otel-collector.hcl" } }
  }
  request "eval-runner" {
    operation = "update"
    path      = "sys/policies/acl/eval-runner"
    data      = { policy = { eval_type = "string", eval_source = "file", path = "/openbao/policies/eval-runner.hcl" } }
  }
}

initialize "identities" {
  request "admin-user" {
    operation = "update"
    path      = "auth/userpass/users/admin"
    data = {
      password       = { eval_type = "string", eval_source = "file", path = "/openbao/bootstrap/admin-password" }
      token_policies = ["admin"]
      token_ttl      = "1h"
      token_max_ttl  = "8h"
    }
  }
  # One AppRole per workload, each bound to exactly one policy (least privilege).
  # Tokens are short-lived; the sidecar agents renew them automatically.
  request "role-langfuse-infra" {
    operation = "update"
    path      = "auth/approle/role/langfuse-infra"
    data      = { token_policies = ["langfuse-infra"], token_ttl = "1h", token_max_ttl = "24h", token_type = "service" }
  }
  request "role-otel-collector" {
    operation = "update"
    path      = "auth/approle/role/otel-collector"
    data      = { token_policies = ["otel-collector"], token_ttl = "1h", token_max_ttl = "24h", token_type = "service" }
  }
  request "role-eval-runner" {
    operation = "update"
    path      = "auth/approle/role/eval-runner"
    data      = { token_policies = ["eval-runner"], token_ttl = "30m", token_max_ttl = "4h", token_type = "service" }
  }
}
