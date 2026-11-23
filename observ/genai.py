"""OpenTelemetry instrumentation for an agent built on the Anthropic SDK + an MCP client.

This is a small, readable version of what "auto-instrumentation" libraries (OpenLLMetry, OpenInference,
the upstream opentelemetry-instrumentation-* packages) do: patch library functions at import time with
`wrapt`, open a span around each call, and fill it with attributes from the OpenTelemetry GenAI semantic
conventions. The agent's own code is never edited.

Spans produced (names, kinds and attributes follow the GenAI and MCP semantic conventions, which are
still marked "Development" and live in github.com/open-telemetry/semantic-conventions-genai):

    invoke_agent ec2-inventory-agent        INTERNAL   one agent run          (agent_span() below)
    ├── chat claude-opus-5-5                CLIENT     one model request      (Anthropic patch)
    ├── tools/call find_instances           CLIENT     one MCP tool call      (MCP patch)
    ├── tools/call fleet_summary            CLIENT     (parallel calls are siblings)
    └── chat claude-opus-5-5                CLIENT     model reads results, answers

Content (prompts, tool arguments, tool results, answers) is opt-in, exactly like the upstream
instrumentations: set OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true. Without it you still get
model, tokens, cost, latency, tool names and errors, which is often all production is allowed to keep.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import wrapt
from opentelemetry import context, propagate, trace
from opentelemetry.metrics import get_meter
from opentelemetry.trace import SpanKind, Status, StatusCode

INSTRUMENTATION_SCOPE = "agent-evals-observ.genai"
PROVIDER = "anthropic"

# USD per million tokens. Cache writes (5-minute TTL) cost 1.25x input; cache reads are much cheaper.
PRICES = {
    "claude-opus-5-5":   {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00,  "cache_read": 0.10, "cache_write": 1.25},
}

tracer = trace.get_tracer(INSTRUMENTATION_SCOPE)
_meter = get_meter(INSTRUMENTATION_SCOPE)
# The two metrics the GenAI conventions define for clients; Prometheus turns them into SLO dashboards.
token_usage = _meter.create_histogram(
    "gen_ai.client.token.usage", unit="{token}", description="Tokens used per model request",
    explicit_bucket_boundaries_advisory=[1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576])
operation_duration = _meter.create_histogram(
    "gen_ai.client.operation.duration", unit="s", description="Duration of model requests and tool calls",
    explicit_bucket_boundaries_advisory=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 80])


def capture_content() -> bool:
    return os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false").lower() == "true"


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    """Cost from the token counts the API returned (never estimated). input_tokens excludes cached tokens."""
    p = PRICES.get(model)
    if p is None:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"] + cache_read * p["cache_read"]
            + cache_write * p["cache_write"]) / 1_000_000


# ------------------------------------------------------------------ message format conversion
def _to_parts(content: Any) -> list[dict]:
    """Anthropic content blocks -> GenAI semconv "parts" (text, tool_call, tool_call_response, reasoning)."""
    if isinstance(content, str):
        return [{"type": "text", "content": content}]
    parts = []
    for block in content or []:
        b = block if isinstance(block, dict) else block.model_dump(exclude_none=True)
        kind = b.get("type")
        if kind == "text":
            parts.append({"type": "text", "content": b.get("text", "")})
        elif kind == "tool_use":
            parts.append({"type": "tool_call", "id": b.get("id"), "name": b.get("name"), "arguments": b.get("input")})
        elif kind == "tool_result":
            parts.append({"type": "tool_call_response", "id": b.get("tool_use_id"), "response": b.get("content")})
        elif kind == "thinking":
            parts.append({"type": "reasoning", "content": b.get("thinking", "")})
        else:
            parts.append({"type": kind or "unknown"})
    return parts


def _messages_attr(messages: list) -> str:
    return json.dumps([{"role": m["role"], "parts": _to_parts(m["content"])} for m in messages], default=str)


def _system_attr(system: Any) -> str:
    if isinstance(system, str):
        return json.dumps([{"type": "text", "content": system}])
    return json.dumps([{"type": "text", "content": b.get("text", "")} for b in system or [] if isinstance(b, dict)])


# ------------------------------------------------------------------ Anthropic Messages API
def _chat_attributes(kwargs: dict) -> dict:
    model = kwargs.get("model", "unknown")
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.request.model": model,
        "server.address": "api.anthropic.com",
    }
    if "max_tokens" in kwargs:
        attrs["gen_ai.request.max_tokens"] = kwargs["max_tokens"]
    effort = (kwargs.get("output_config") or {}).get("effort")
    if effort:
        attrs["gen_ai.request.effort"] = effort       # not (yet) in the conventions; harmless extra
    if capture_content():
        if kwargs.get("system"):
            attrs["gen_ai.system_instructions"] = _system_attr(kwargs["system"])
        attrs["gen_ai.input.messages"] = _messages_attr(kwargs.get("messages", []))
        if kwargs.get("tools"):
            attrs["gen_ai.tool.definitions"] = json.dumps(
                [{"type": "function", "name": t.get("name"), "description": t.get("description"),
                  "parameters": t.get("input_schema")} for t in kwargs["tools"] if isinstance(t, dict)])
    return attrs


def _record_response(span, message: Any, request_model: str, elapsed: float) -> None:
    usage = message.usage
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    # The conventions say gen_ai.usage.input_tokens includes cached tokens; Anthropic reports them apart.
    total_in = usage.input_tokens + cache_read + cache_write
    cost = cost_usd(request_model, usage.input_tokens, usage.output_tokens, cache_read, cache_write)
    span.set_attributes({
        "gen_ai.response.model": message.model,
        "gen_ai.response.id": message.id,
        "gen_ai.response.finish_reasons": [message.stop_reason or "unknown"],
        "gen_ai.usage.input_tokens": total_in,
        "gen_ai.usage.output_tokens": usage.output_tokens,
        "gen_ai.usage.cache_read.input_tokens": cache_read,
        "gen_ai.usage.cache_write.input_tokens": cache_write,
        "gen_ai.usage.cost": cost,   # Langfuse reads this; not part of the OTel conventions
    })
    if capture_content():
        span.set_attribute("gen_ai.output.messages", json.dumps(
            [{"role": "assistant", "parts": _to_parts(message.content),
              "finish_reason": message.stop_reason}], default=str))
    if message.stop_reason == "refusal":
        span.set_status(Status(StatusCode.ERROR, "model refused"))
        span.set_attribute("error.type", "refusal")
    metric_attrs = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": PROVIDER,
                    "gen_ai.request.model": request_model, "gen_ai.response.model": message.model}
    token_usage.record(total_in, {**metric_attrs, "gen_ai.token.type": "input"})
    token_usage.record(usage.output_tokens, {**metric_attrs, "gen_ai.token.type": "output"})
    operation_duration.record(elapsed, metric_attrs)


def _unwrap_message(result: Any) -> Any:
    """`.with_raw_response.create()` (used by LangChain) returns a raw response; .parse() is cached."""
    return result if hasattr(result, "usage") else result.parse()


@contextmanager
def _chat_span(kwargs: dict) -> Iterator[Any]:
    attrs = _chat_attributes(kwargs)
    with tracer.start_as_current_span(f"chat {attrs['gen_ai.request.model']}", kind=SpanKind.CLIENT,
                                      attributes=attrs, record_exception=False) as span:
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            span.set_attribute("error.type", type(e).__qualname__)
            operation_duration.record(0, {"gen_ai.operation.name": "chat", "gen_ai.provider.name": PROVIDER,
                                          "gen_ai.request.model": attrs["gen_ai.request.model"],
                                          "error.type": type(e).__qualname__})
            raise


async def _async_create(wrapped, instance, args, kwargs):
    if kwargs.get("stream"):
        return await wrapped(*args, **kwargs)   # streaming responses are not covered by this small instrumentor
    with _chat_span(kwargs) as span:
        t0 = time.perf_counter()
        result = await wrapped(*args, **kwargs)
        _record_response(span, _unwrap_message(result), kwargs.get("model", "unknown"), time.perf_counter() - t0)
        return result


def _sync_create(wrapped, instance, args, kwargs):
    if kwargs.get("stream"):
        return wrapped(*args, **kwargs)
    with _chat_span(kwargs) as span:
        t0 = time.perf_counter()
        result = wrapped(*args, **kwargs)
        _record_response(span, _unwrap_message(result), kwargs.get("model", "unknown"), time.perf_counter() - t0)
        return result


# ------------------------------------------------------------------ MCP client tool calls
async def _call_tool(wrapped, instance, args, kwargs):
    name = args[0] if args else kwargs["name"]
    arguments = args[1] if len(args) > 1 else kwargs.get("arguments")
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.type": "extension",         # an MCP tool lives outside the agent process
        "mcp.method.name": "tools/call",
    }
    if capture_content():
        attrs["gen_ai.tool.call.arguments"] = json.dumps(arguments, default=str)
    with tracer.start_as_current_span(f"tools/call {name}", kind=SpanKind.CLIENT, attributes=attrs,
                                      record_exception=False) as span:
        # Trace context crosses the MCP boundary inside params._meta (W3C traceparent), so an instrumented
        # MCP server would continue this same trace. The HTTP headers alone are not enough: one MCP request
        # can span several HTTP requests and vice versa.
        meta = dict(kwargs.get("meta") or {})
        propagate.inject(meta)
        kwargs["meta"] = meta
        t0 = time.perf_counter()
        try:
            result = await wrapped(*args, **kwargs)
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            span.set_attribute("error.type", type(e).__qualname__)
            raise
        finally:
            operation_duration.record(time.perf_counter() - t0, {"gen_ai.operation.name": "execute_tool",
                                                                 "gen_ai.tool.name": name})
        text = json.dumps(result.structuredContent, default=str) if result.structuredContent is not None else \
            "\n".join(getattr(c, "text", "") for c in result.content)
        span.set_attribute("mcp.tool.result.size", len(text))
        if result.isError:
            # A tool error is not an exception: the model gets the message and may recover. Still an ERROR span,
            # because "tool errors per minute" is one of the first agent SLOs you want.
            span.set_status(Status(StatusCode.ERROR, text[:200]))
            span.set_attribute("error.type", "tool_error")
        if capture_content():
            span.set_attribute("gen_ai.tool.call.result", text)
        return result


_instrumented = False


def instrument() -> None:
    """Patch the Anthropic SDK and the MCP client. Call once, before the agent creates its clients."""
    global _instrumented
    if _instrumented:
        return
    for module in ("anthropic.resources.messages.messages", "anthropic.resources.beta.messages.messages"):
        wrapt.wrap_function_wrapper(module, "AsyncMessages.create", _async_create)
        wrapt.wrap_function_wrapper(module, "Messages.create", _sync_create)
    wrapt.wrap_function_wrapper("mcp.client.session", "ClientSession.call_tool", _call_tool)
    _instrumented = True


# ------------------------------------------------------------------ the agent run itself
@contextmanager
def agent_span(*, agent_name: str, question: str, model: str, conversation_id: str | None = None,
               user_id: str | None = None, agent_version: str = "1", extra: dict | None = None) -> Iterator[Any]:
    """The root `invoke_agent` span. Wrap one call of the agent's entry point in it.

    session.id / user.id go into baggage, so BaggageSpanProcessor stamps them on every child span too
    (Langfuse groups traces into sessions and users by them).
    """
    attrs = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": PROVIDER,
        "gen_ai.agent.name": agent_name,
        "gen_ai.agent.version": agent_version,
        "gen_ai.request.model": model,
        **(extra or {}),
    }
    if conversation_id:
        attrs["gen_ai.conversation.id"] = conversation_id
    if capture_content():
        attrs["gen_ai.input.messages"] = json.dumps([{"role": "user", "parts": [{"type": "text", "content": question}]}])
    ctx = context.get_current()
    from opentelemetry import baggage
    if conversation_id:
        ctx = baggage.set_baggage("session.id", conversation_id, ctx)
    if user_id:
        ctx = baggage.set_baggage("user.id", user_id, ctx)
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span(f"invoke_agent {agent_name}", kind=SpanKind.INTERNAL, attributes=attrs,
                                          record_exception=False) as span:
            t0 = time.perf_counter()
            try:
                yield span
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
                span.set_attribute("error.type", type(e).__qualname__)
                raise
            finally:
                operation_duration.record(time.perf_counter() - t0, {"gen_ai.operation.name": "invoke_agent",
                                                                     "gen_ai.agent.name": agent_name})
    finally:
        context.detach(token)


def set_agent_output(span, answer: str) -> None:
    if capture_content():
        span.set_attribute("gen_ai.output.messages", json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": answer}], "finish_reason": "stop"}]))
