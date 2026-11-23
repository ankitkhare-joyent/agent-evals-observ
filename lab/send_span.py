"""Labs 02-04: send one hand-made trace through the Collector, with no agent and no API call.

    mise run -q lab:span                 # a normal span
    mise run -q lab:span -- --leak       # the same span with a fake Anthropic key and a bearer token in it

It prints the trace id so you can look the trace up in Langfuse (lf_trace <id>) and see what arrived.
"""
import argparse

from opentelemetry import trace

from observ.telemetry import setup_telemetry

FAKE_KEY = "sk-ant-api03-" + "FAKE0123456789abcdefFAKE0123456789"      # shaped like a key, not a real one
FAKE_JWT = "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJsYWIifQ.c2lnbmF0dXJl"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--leak", action="store_true", help="put a fake API key and bearer token into the span")
    args = p.parse_args()
    telemetry = setup_telemetry(service_name="lab-send-span", environment="development")
    tracer = trace.get_tracer("lab")
    with tracer.start_as_current_span("invoke_agent lab-agent", attributes={
        "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "lab-agent", "gen_ai.provider.name": "anthropic",
    }) as root:
        with tracer.start_as_current_span("tools/call echo", attributes={
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "echo",
            "gen_ai.tool.call.arguments": '{"text": "hello"}',
            "gen_ai.tool.call.result": (f"config dump: ANTHROPIC_API_KEY={FAKE_KEY} auth={FAKE_JWT}"
                                        if args.leak else "hello"),
        }):
            pass
        trace_id = format(root.get_span_context().trace_id, "032x")
    telemetry.shutdown()
    print(trace_id)


if __name__ == "__main__":
    main()
