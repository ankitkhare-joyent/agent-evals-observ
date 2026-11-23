"""Instrumentation tests with no network: the Anthropic SDK talks to a mock HTTP transport, spans go to an
in-memory exporter, and we assert the GenAI semantic-convention attributes are there."""
import json

import anthropic
import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from evals.trajectory import build_trajectory
from observ import genai

MESSAGE = {
    "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "content": [{"type": "text", "text": "Account 392918273645 has the most running vCPUs."}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900,
              "cache_creation_input_tokens": 0},
}


@pytest.fixture(scope="module")
def exporter():
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(tp)
    genai.instrument()
    return exp


async def test_chat_span_has_genai_attributes(exporter, monkeypatch):
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    exporter.clear()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=MESSAGE))
    client = anthropic.AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=transport))
    with genai.agent_span(agent_name="test-agent", question="who has most vCPUs?", model="claude-opus-5-5",
                          conversation_id="c1") as span:
        msg = await client.messages.create(model="claude-opus-5-5", max_tokens=100, system="be brief",
                                           messages=[{"role": "user", "content": "who has most vCPUs?"}])
        genai.set_agent_output(span, msg.content[0].text)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    chat = spans["chat claude-opus-5-5"]
    a = chat.attributes
    assert a["gen_ai.operation.name"] == "chat" and a["gen_ai.provider.name"] == "anthropic"
    assert a["gen_ai.usage.input_tokens"] == 1000          # includes cached tokens, per the conventions
    assert a["gen_ai.usage.cache_read.input_tokens"] == 900
    assert a["gen_ai.response.finish_reasons"] == ("end_turn",)
    assert json.loads(a["gen_ai.input.messages"])[0]["parts"][0]["content"] == "who has most vCPUs?"
    assert chat.parent.span_id == spans["invoke_agent test-agent"].context.span_id
    # cost from real token counts: 100*$4 + 20*$20 + 900*$0.20 per million
    assert a["gen_ai.usage.cost"] == pytest.approx((100 * 4 + 20 * 20 + 900 * 0.2) / 1e6)

    t = build_trajectory(list(exporter.get_finished_spans()))
    assert t.model_calls == 1 and t.input_tokens == 1000 and t.latency_s > 0


async def test_content_is_not_recorded_by_default(exporter, monkeypatch):
    monkeypatch.delenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False)
    exporter.clear()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=MESSAGE))
    client = anthropic.AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=transport))
    await client.messages.create(model="claude-opus-5-5", max_tokens=100,
                                 messages=[{"role": "user", "content": "secret question"}])
    (chat,) = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in chat.attributes and "gen_ai.output.messages" not in chat.attributes
    assert chat.attributes["gen_ai.usage.output_tokens"] == 20


async def test_api_error_marks_span_failed(exporter):
    exporter.clear()
    transport = httpx.MockTransport(lambda req: httpx.Response(529, json={"type": "error", "error": {
        "type": "overloaded_error", "message": "Overloaded"}}))
    client = anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                      http_client=httpx.AsyncClient(transport=transport))
    with pytest.raises(anthropic.APIStatusError):
        await client.messages.create(model="claude-opus-5-5", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    (chat,) = exporter.get_finished_spans()
    assert chat.status.status_code.name == "ERROR" and chat.attributes["error.type"]
