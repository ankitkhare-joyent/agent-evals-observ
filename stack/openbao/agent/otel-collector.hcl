# OpenBao Agent sidecar for the OpenTelemetry Collector. Different AppRole, different policy:
# it can read the Langfuse ingestion key and nothing else.
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
      remove_secret_id_file_after_reading = false
    }
  }
}

template_config {
  exit_on_retry_failure = true
  static_secret_render_interval = "5m"
}

# The full HTTP header value, so the Collector config can reference the file as-is: ${file:...}
template {
  contents    = "{{- with secret \"secret/data/langfuse/otlp\" -}}Basic {{ printf \"%s:%s\" .Data.data.public_key .Data.data.secret_key | base64Encode }}{{- end -}}"
  destination = "/secrets/langfuse_otlp_authorization"
  perms       = "0444"
}
