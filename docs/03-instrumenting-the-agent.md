# 03 · Instrumenting the agent without touching it

The agent lives in `../learn-ai-agent` and we treat it as someone else's code. Not one line of it is
edited, yet every model call and tool call shows up as a span. This is how auto-instrumentation works,
and it is worth seeing once in ~250 lines: [observ/genai.py](../observ/genai.py).

## The trick: patch the library, not the app

The agent makes model calls through `anthropic.AsyncAnthropic().messages.create(...)` and tool calls
through `mcp.ClientSession.call_tool(...)`. Both are methods on classes. `wrapt` replaces those methods,
at runtime, with a wrapper that opens a span, calls the original, and records the result:

```python
wrapt.wrap_function_wrapper("anthropic.resources.messages.messages", "AsyncMessages.create", _async_create)
wrapt.wrap_function_wrapper("mcp.client.session", "ClientSession.call_tool", _call_tool)

async def _async_create(wrapped, instance, args, kwargs):
    with tracer.start_as_current_span(f"chat {kwargs['model']}", kind=SpanKind.CLIENT, attributes=...) as span:
        result = await wrapped(*args, **kwargs)          # the real API call
        span.set_attributes({"gen_ai.usage.input_tokens": ..., "gen_ai.response.finish_reasons": ...})
        return result
```

Patching the *class* means every client created later is covered, including the ones LangChain
creates inside `ChatAnthropic`. That is why one instrumentation covers both agent variants. (LangChain
calls `.with_raw_response.create()`, which returns a raw HTTP response; `_unwrap_message` handles it.)

This is exactly what the published packages do: `opentelemetry-instrumentation-anthropic` (Traceloop's
OpenLLMetry), `openinference-instrumentation-anthropic` (Arize), and the upstream
`opentelemetry-instrumentation-openai-v2` family. In production you would normally install one of them.
We wrote our own because (a) you can read it, (b) the upstream repo has no Anthropic instrumentation yet,
and (c) the community ones still emit some older attribute names. Same idea either way.

## The root span

Libraries can't know where "one agent run" starts and ends, so the application opens that span:

```python
with genai.agent_span(agent_name="ec2-inventory-agent", question=q, model=MODEL,
                      conversation_id=session, user_id="ankit") as span:
    answer = await ask(q)                 # the agent's own entry point, unchanged
    genai.set_agent_output(span, answer)
```

`session.id` and `user.id` go into **baggage**, and `BaggageSpanProcessor` copies them onto every
child span, so Langfuse can group traces into sessions and users even though the `chat` spans were
created deep inside the Anthropic SDK.

## What gets recorded, and what doesn't by default

Always: model, token counts (incl. cache reads), cost in USD from the real token counts, latency,
finish reason, tool names, tool errors, result sizes, exceptions. Only with
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`: system prompt, messages, tool definitions,
tool arguments and results, the final answer.

In production, content capture is a data-governance decision. Common patterns: capture everything in
dev and staging; in prod capture content for a sampled fraction, or capture but redact in the Collector
(lesson 04), or send content to separate, shorter-retention storage. Metadata alone already answers
"why was this slow / expensive / failing".

Tool errors deserve a note. When an MCP tool returns `isError: true` it is not an exception: the model
reads the error and may recover. We still mark the span `ERROR` with `error.type = tool_error`, because
"tool error rate" is one of the first SLOs you want (lesson 09).

## Cost tracking

`cost_usd()` multiplies the API's reported token counts by the price table
(Opus 5.5: $4 / $20 per million input/output tokens, cache reads $0.20). Never estimate tokens from
string length; it can be off by enough to reverse a comparison. Prompt caching matters: the agent
re-sends the system prompt and tool list every step, and caching makes those re-reads ~20x cheaper.
`gen_ai.usage.cache_read.input_tokens` tells you whether caching is working.

## Try it

```bash
mise run trace -- "Which account has the most running vCPUs right now?"
mise run trace -- --variant langgraph "What is the largest instance in account 392918273645?"
mise run trace -- --session demo --no-content "Tell me about i-0abc..."   # metadata only
```

Open the printed link. In Langfuse you will see the `invoke_agent` root, the `chat` generations with
tokens and cost, and the `tools/call` spans with arguments and results. Compare the two variants'
trees: same model calls and tool calls underneath, different framework on top. That is the point of
standard instrumentation.

Run the unit tests to see the instrumentation checked without any network:

```bash
mise run test        # tests/test_instrumentation.py uses a mock HTTP transport + in-memory exporter
```

Next: [04 · The Collector pipeline](04-collector-pipeline.md)
