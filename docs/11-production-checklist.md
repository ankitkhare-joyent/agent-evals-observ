# 11 · Production checklist for agent evals and observability

A one-page summary of the standards from lessons 01-10. Use it as a review list for any agent you ship.

## Instrumentation
- [ ] OpenTelemetry with the GenAI conventions (`invoke_agent`, `chat`, `execute_tool`), in one module.
- [ ] `gen_ai.provider.name`, request/response model, finish reason, input/output/cache tokens on every model span.
- [ ] Cost computed from API token counts, never estimated.
- [ ] Tool errors marked as span errors even when the model recovers.
- [ ] Session and user IDs on every span (baggage), so traces group into conversations.
- [ ] Content capture is a deliberate, documented decision per environment.
- [ ] Trace context propagated into MCP calls (`params._meta.traceparent`), and MCP servers instrumented.
- [ ] Instrumentation and semconv versions pinned (the conventions are still "Development").

## Pipeline
- [ ] Apps export OTLP to a Collector, never straight to a vendor.
- [ ] `memory_limiter` first; redaction of keys/tokens/PII before export; attribute size caps.
- [ ] RED metrics derived from spans *before* sampling.
- [ ] Tail sampling keeps all errors, slow runs and eval runs.
- [ ] Backend credentials only in the Collector, delivered by the secrets manager.
- [ ] Sending queue and retries; persistent queue if loss matters.

## Offline evals
- [ ] Golden cases computed from a source of truth, plus a stable hand-verified core.
- [ ] Coverage both ways: must-answer, must-refuse, must-not-act, must-not-call-tools.
- [ ] Code graders first; atomic metrics; outcomes strict, trajectories loose (except safety).
- [ ] Graders tested with known-good and known-bad answers in CI.
- [ ] Reps per case; report pass@k and pass^k; confidence intervals on every rate.
- [ ] Infra errors and stale ground truth excluded from scores and reported separately.
- [ ] The real agent entry point, same model, prompt and tools as production.
- [ ] Full trajectories stored per run; failures read by a human before trusting the number.
- [ ] Cost and latency reported next to quality.
- [ ] CI gate: strict on never-events, looser on averages; eval data pinned (e.g. Iceberg snapshot).

## LLM judges
- [ ] Binary criteria, reasoning before verdict, structured output.
- [ ] Inputs fenced as untrusted data; told not to reward length.
- [ ] Different model (ideally family) from the agent; jury for close calls.
- [ ] Calibrated against human labels (agreement, kappa), re-calibrated on every judge change.
- [ ] Judge failures and judge cost tracked separately.

## Production monitoring
- [ ] SLOs and alerts: tool error rate, p95 run latency, model calls per run, token burn.
- [ ] Sampled online judging of real traces, written back as scores.
- [ ] User feedback captured as scores on the trace.
- [ ] Low-scoring traces reviewed weekly and turned into eval cases.

## Secrets
- [ ] An open source secrets manager (OpenBao) with KMS auto-unseal, Raft HA, TLS, audit to SIEM.
- [ ] Workload identity (Kubernetes / AWS / OIDC) instead of static secret-zero where possible.
- [ ] One identity and one least-privilege policy per workload.
- [ ] Secrets delivered to files in memory (sidecar/CSI), never in images, compose files, env files in git.
- [ ] Short token TTLs with renewal; dynamic credentials where the engine exists; KV versions for rotation.
- [ ] No root token at rest.

Next: [12 · Architecture](12-architecture.md) (the full picture, drawn last)
