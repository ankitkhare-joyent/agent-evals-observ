# 02 · OpenTelemetry and the GenAI semantic conventions

## OpenTelemetry in five minutes

OpenTelemetry (OTel) is the CNCF standard for telemetry. It is to observability what Iceberg is to
table formats: an open spec that every engine and vendor speaks, so you are never locked in.

- **Signals**: traces (spans), metrics (counters, histograms), logs. We use traces and metrics.
- **API vs SDK**: libraries call the *API* (`trace.get_tracer(...)`, `start_as_current_span`). Your
  application installs the *SDK* (a `TracerProvider` with processors and exporters). No SDK installed means
  every API call is a no-op, which is why instrumented libraries cost nothing when tracing is off.
- **Context propagation**: the current span lives in a context variable. Child spans attach to it
  automatically, even across `asyncio.gather`. Across process boundaries the context travels as a
  W3C `traceparent` header (`00-<trace id>-<span id>-01`).
- **Baggage**: key-value pairs that travel with the context. We use it to stamp `session.id`,
  `user.id` and experiment IDs onto every span (`BaggageSpanProcessor` in
  [observ/telemetry.py](../observ/telemetry.py)).
- **Resource**: who emitted the data (`service.name`, `service.version`, `deployment.environment.name`).
- **OTLP**: the wire protocol (gRPC on 4317, HTTP on 4318). Apps send OTLP to a **Collector** (lesson 04),
  which sends it on to backends.

## Semantic conventions: agreeing on attribute names

A span is only useful if every tool agrees what its attributes mean. *Semantic conventions* are that
agreement: HTTP spans have `http.request.method`, database spans have `db.query.text`, and so on.

For LLMs and agents the **GenAI semantic conventions** define:

| Span | `gen_ai.operation.name` | Name format | Kind |
|---|---|---|---|
| Agent run | `invoke_agent` | `invoke_agent {gen_ai.agent.name}` | INTERNAL (in-process) or CLIENT (remote agent) |
| Model call | `chat` | `chat {gen_ai.request.model}` | CLIENT |
| Tool call | `execute_tool` | `execute_tool {gen_ai.tool.name}` (MCP: `tools/call {tool}`) | INTERNAL / CLIENT |
| Others | `create_agent`, `invoke_workflow`, `plan`, `retrieval`, `embeddings`, memory ops | | |

Key attributes you will see in our traces:

```
gen_ai.provider.name                  anthropic                (replaced the old gen_ai.system)
gen_ai.request.model                  claude-opus-5-5
gen_ai.response.model / .id           what actually served it
gen_ai.response.finish_reasons        ["tool_use"] / ["end_turn"] / ["refusal"]
gen_ai.usage.input_tokens             INCLUDES cached tokens (Anthropic reports them separately - we add them)
gen_ai.usage.cache_read.input_tokens  prompt-cache hits
gen_ai.usage.output_tokens
gen_ai.agent.name / .version          ec2-inventory-agent
gen_ai.conversation.id                session/thread id
gen_ai.tool.name / gen_ai.tool.type   find_instances / extension
error.type                            exception class, "tool_error", or "refusal"
```

Content attributes are **opt-in** because they hold user data:
`gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.tool.definitions`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`.
Messages use a structured "parts" format (`text`, `tool_call`, `tool_call_response`, `reasoning`), so a
backend can render any provider's conversation the same way. The switch everyone uses is
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`.

Two metrics are defined for clients: `gen_ai.client.token.usage` (histogram, split by
`gen_ai.token.type` = input/output) and `gen_ai.client.operation.duration` (seconds).

## MCP has conventions too

MCP calls are JSON-RPC, and one MCP request can span several HTTP requests (or the reverse), so HTTP
headers are not enough to carry the trace. The MCP conventions put `traceparent` inside the request's
`params._meta`. Our instrumentation does exactly that in `_call_tool`, so an instrumented MCP server
would continue the agent's trace and you would see the server-side span (and the REST API call behind
it) in the same tree:

```
invoke_agent ec2-inventory-agent
├── chat claude-opus-5-5
├── tools/call find_instances (CLIENT)           <- our span, in the agent process
│   └── tools/call find_instances (SERVER)       <- would come from the MCP server, same trace id
│       └── GET /v1/ec2/instances                <- and the FastAPI span behind it
└── chat claude-opus-5-5
```

## Status in October 2026: read this before you rely on it

- Every `gen_ai.*` attribute is still **Development** stability. Names can still change.
- In June 2026 the GenAI and MCP conventions moved out of the main repo into
  `open-telemetry/semantic-conventions-genai` (semconv v1.42.0 deprecated the in-repo copies). That repo
  has no tagged release yet, so pin your instrumentation library versions, not a schema URL.
- The core shape (operation name, provider, model, token usage) has been stable since v1.37 and every
  major backend (Langfuse, Phoenix, Datadog, Grafana, Honeycomb, New Relic) ingests it.
- Instrumentation libraries lag the spec. Many still emit `gen_ai.system` or vendor formats
  (OpenInference's `llm.*`, Traceloop's older names). Backends map several dialects; check what yours reads.

What this means for you: instrument with the conventions, keep the instrumentation in one module
(we have [observ/genai.py](../observ/genai.py)), and expect a small migration now and then. That is far
better than a vendor SDK in every file.

Next: [03 · Instrumenting the agent without touching it](03-instrumenting-the-agent.md)
