# OpenBao Agent sidecar for the Langfuse stack (the "Vault Agent injector" pattern, in Compose form).
# It logs in with its own AppRole, renders secrets into a tmpfs volume shared with Langfuse's containers,
# keeps its token renewed, and re-renders if a secret changes. The apps never talk to OpenBao themselves.
pid_file = "/tmp/bao-agent.pid"

vault {
  address = "http://openbao:8200"
  retry { num_retries = 10 }
}

auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/openbao/approle/role_id"
      secret_id_file_path                 = "/openbao/approle/secret_id"
      remove_secret_id_file_after_reading = false   # production: true, with a wrapped, single-use secret_id
    }
  }
}

template_config {
  exit_on_retry_failure = true
  static_secret_render_interval = "5m"   # how often KV (static) secrets are re-read to pick up rotations
}

template {
  source      = "/openbao/templates/langfuse.env.ctmpl"
  destination = "/secrets/langfuse.env"
  perms       = "0444"
}
template {
  contents    = "{{ with secret \"secret/data/langfuse/infra\" }}{{ .Data.data.postgres_password }}{{ end }}"
  destination = "/secrets/postgres_password"
  perms       = "0444"
}
template {
  contents    = "{{ with secret \"secret/data/langfuse/infra\" }}{{ .Data.data.clickhouse_password }}{{ end }}"
  destination = "/secrets/clickhouse_password"
  perms       = "0444"
}
template {
  source      = "/openbao/templates/redis.conf.ctmpl"
  destination = "/secrets/redis.conf"
  perms       = "0444"
}
template {
  contents    = "{{ with secret \"secret/data/langfuse/infra\" }}{{ .Data.data.minio_password }}{{ end }}"
  destination = "/secrets/minio_password"
  perms       = "0444"
}
