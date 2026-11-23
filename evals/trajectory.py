"""Turn an agent run's spans into a trajectory the graders can check.

This is "trace-based evaluation": the same telemetry you ship to Langfuse for observability is what the
graders read (which tools ran, with which arguments, what came back, tokens, cost, latency). There is no
second, eval-only logging path that could drift from what production records.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import StatusCode


class SpanRecorder(SpanProcessor):
    """Keeps finished spans in memory, grouped by trace id, until the runner takes them."""

    def __init__(self) -> None:
        self._spans: dict[int, list[ReadableSpan]] = defaultdict(list)
        self._lock = threading.Lock()

    def on_end(self, span: ReadableSpan) -> None:
        with self._lock:
            self._spans[span.context.trace_id].append(span)

    def pop(self, trace_id: int) -> list[ReadableSpan]:
        with self._lock:
            return self._spans.pop(trace_id, [])

    def shutdown(self) -> None: ...

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result: str
    is_error: bool
    duration_s: float


@dataclass
class Trajectory:
    tool_calls: list[ToolCall] = field(default_factory=list)
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    models: set[str] = field(default_factory=set)
    finish_reasons: list[str] = field(default_factory=list)

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tool_calls]

    @property
    def tool_errors(self) -> int:
        return sum(t.is_error for t in self.tool_calls)

    def tool_results_text(self) -> str:
        return "\n".join(t.result for t in self.tool_calls)

    def to_dict(self) -> dict:
        return {
            "tool_calls": [t.__dict__ for t in self.tool_calls],
            "model_calls": self.model_calls, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": round(self.cost_usd, 6), "latency_s": round(self.latency_s, 3),
            "models": sorted(self.models), "finish_reasons": self.finish_reasons,
        }


def _json(value, default):
    try:
        return json.loads(value) if isinstance(value, str) else default
    except json.JSONDecodeError:
        return default


def build_trajectory(spans: list[ReadableSpan]) -> Trajectory:
    t = Trajectory()
    for s in sorted(spans, key=lambda s: s.start_time or 0):
        a = s.attributes or {}
        op = a.get("gen_ai.operation.name")
        duration = ((s.end_time or 0) - (s.start_time or 0)) / 1e9
        if op == "invoke_agent":
            t.latency_s = duration
        elif op == "chat":
            t.model_calls += 1
            t.input_tokens += int(a.get("gen_ai.usage.input_tokens", 0))
            t.output_tokens += int(a.get("gen_ai.usage.output_tokens", 0))
            t.cache_read_tokens += int(a.get("gen_ai.usage.cache_read.input_tokens", 0))
            t.cost_usd += float(a.get("gen_ai.usage.cost", 0.0))
            if a.get("gen_ai.response.model"):
                t.models.add(str(a["gen_ai.response.model"]))
            t.finish_reasons.extend(a.get("gen_ai.response.finish_reasons", []))
        elif op == "execute_tool":
            t.tool_calls.append(ToolCall(
                name=str(a.get("gen_ai.tool.name")),
                arguments=_json(a.get("gen_ai.tool.call.arguments"), {}),
                result=str(a.get("gen_ai.tool.call.result", "")),
                is_error=s.status.status_code == StatusCode.ERROR,
                duration_s=round(duration, 3),
            ))
    return t
