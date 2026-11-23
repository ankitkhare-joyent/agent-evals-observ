# Langfuse's own infrastructure (Postgres, ClickHouse, Redis, MinIO, web, worker) reads exactly one secret.
path "secret/data/langfuse/infra" { capabilities = ["read"] }
