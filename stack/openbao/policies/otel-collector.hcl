# The Collector needs only the Langfuse ingestion key (to authenticate its OTLP exporter).
path "secret/data/langfuse/otlp" { capabilities = ["read"] }
