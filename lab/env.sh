# Source this at the start of every lab sitting (bash or zsh), from the repo root:
#
#   source lab/env.sh
#
# It only sets variables and defines shell functions. Nothing here prints a secret value. The functions that
# return one (bao_field, approle_token) are meant to be used inside $( ... ) only.

# Names start with EVALS_ on purpose: the agent under test reads BAO_ADDR / BAO_TOKEN to find *its own* OpenBao
# (the lakehouse's, on :8200). Exporting those names here would point it at ours, and its login would fail.
export EVALS_BAO_ADDR="http://127.0.0.1:38200"  # this project's OpenBao
export LF="http://localhost:33000"              # Langfuse
export PROM="http://localhost:39095"            # Prometheus
export OTELCOL="http://localhost:38888"         # the Collector's own metrics
export OTLP="http://localhost:34318"            # where apps send OTLP

# Log in as the human admin (userpass) with the password scripts/bootstrap.sh generated. The token lasts 1 hour;
# source this file again to get a fresh one.
if [ -s .bao/bootstrap/admin-password ]; then
  export EVALS_BAO_TOKEN="$(jq -n --rawfile p .bao/bootstrap/admin-password '{password: ($p | rtrimstr("\n"))}' \
    | curl -s -X POST -d @- "$EVALS_BAO_ADDR/v1/auth/userpass/login/admin" | jq -r '.auth.client_token // empty')"
  [ -n "$EVALS_BAO_TOKEN" ] || echo "lab/env.sh: admin login failed - is the stack up? (mise run up)" >&2
else
  echo "lab/env.sh: no .bao/bootstrap/admin-password - run from the repo root after 'mise run setup'" >&2
fi

# OpenBao ---------------------------------------------------------------------------------------------------
# bao_keys langfuse/api                  -> a secret's field names and version, never the values
bao_keys() {
  curl -s -H "X-Vault-Token: ${TOKEN:-$EVALS_BAO_TOKEN}" "$EVALS_BAO_ADDR/v1/secret/data/$1" \
    | jq -c 'if .errors then {errors} else {fields: (.data.data | keys), version: .data.metadata.version} end'
}
# PW=$(bao_field langfuse/infra postgres_password)   -> one value, for use inside $( ... )
bao_field() {
  curl -s -H "X-Vault-Token: ${TOKEN:-$EVALS_BAO_TOKEN}" "$EVALS_BAO_ADDR/v1/secret/data/$1" | jq -r --arg f "$2" '.data.data[$f] // empty'
}
# TOKEN=$(approle_token eval-runner)      -> log in as a workload, with its role_id + secret_id from .bao/approle/
# Then TOKEN=... bao_keys <path> runs as that workload instead of as admin.
approle_token() {
  jq -n --rawfile r ".bao/approle/$1/role_id" --rawfile s ".bao/approle/$1/secret_id" '{role_id: $r, secret_id: $s}' \
    | curl -s -X POST -d @- "$EVALS_BAO_ADDR/v1/auth/approle/login" | jq -r '.auth.client_token // empty'
}

# Langfuse --------------------------------------------------------------------------------------------------
# lf_get api/public/v2/observations 'limit=5&fields=core,basic'   -> GET with the project's API key (Basic auth)
lf_get() {
  curl -s -u "$(bao_field langfuse/api public_key):$(bao_field langfuse/api secret_key)" "$LF/$1?${2:-}"
}
# lf_trace <trace id>                     -> the trace as a compact list of observations, in start order
lf_trace() {
  lf_get api/public/v2/observations "traceId=$1&fields=core,basic,model,usage&limit=100" | jq -r '
    .data | sort_by(.startTime)[] |
    [.type, .name, (.model // ""), (.usageDetails.input // "" | tostring), (.usageDetails.output // "" | tostring),
     (.totalCost // "" | tostring)] | @tsv' | column -t -s $'\t'
}
# lf_last [environment]                   -> the id of the newest agent trace (default environment: development)
lf_last() {
  lf_get api/public/v2/observations "type=AGENT&environment=${1:-development}&fields=core&limit=1" | jq -r '.data[0].traceId // empty'
}
# lf_scores <trace id> [full]             -> every score on a trace; with "full", the comment (the judge's reasoning)
lf_scores() {
  if [ "${2:-}" = full ]; then
    lf_get api/public/v3/scores "traceId=$1&limit=50&fields=core,details" | jq -r '.data[] | "\(.name) = \(.value)\n    \(.comment // "" | .[0:400])"'
  else
    lf_get api/public/v3/scores "traceId=$1&limit=50" | jq -r '.data[] | "\(.name)\t\(.value)"' | sort | column -t
  fi
}
# ch "SELECT ..."                         -> SQL on Langfuse's ClickHouse (password read inside the container)
ch() {
  docker compose exec -T clickhouse sh -c 'clickhouse-client -u clickhouse --password "$(cat /secrets/clickhouse_password)" --format PrettyCompactMonoBlock -q "$0"' "$1"
}

# Metrics ---------------------------------------------------------------------------------------------------
# prom 'sum(agent_calls_total)'           -> an instant PromQL query, one line per series
prom() {
  curl -s "$PROM/api/v1/query" --data-urlencode "query=$1" \
    | jq -r '.data.result[] | "\(.metric | del(.__name__) | to_entries | map("\(.key)=\(.value)") | join(" "))  \(.value[1])"'
}
# otel_stats                              -> spans in, refused, and out of the Collector, per component
otel_stats() {
  curl -s "$OTELCOL/metrics" | grep -E '^otelcol_(receiver_accepted_spans|receiver_refused_spans|exporter_sent_spans|exporter_send_failed_spans|processor_tail_sampling_global_count_traces_sampled)' \
    | sed -E 's/service_instance_id="[^"]*",?//; s/service_name="[^"]*",?//; s/service_version="[^"]*",?//; s/otel_scope_[a-z_]*="[^"]*",?//g'
}
