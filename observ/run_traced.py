"""Run the agent once with full tracing, then print the answer and a link to its trace in Langfuse.

    mise run trace -- "which account has the most running vCPUs?"
    mise run trace -- --variant langgraph --session demo-1 "and its largest instance?"

Nothing in ../learn-ai-agent is modified: instrumentation patches the Anthropic SDK and the MCP client
in this process before the agent creates them.
"""
import argparse
import asyncio
import os
import uuid

from observ import genai
from observ.agent_under_test import VARIANTS, agent_model, run_agent
from observ.secrets import load_eval_runner_secrets
from observ.telemetry import setup_telemetry

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:33000")
LANGFUSE_PROJECT = "ec2-agent"


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("question", nargs="+")
    p.add_argument("--variant", choices=VARIANTS, default="raw_loop")
    p.add_argument("--session", help="conversation/session id (groups traces in Langfuse)")
    p.add_argument("--user", default="ankit")
    p.add_argument("--no-content", action="store_true", help="record metadata only, no prompts/results")
    args = p.parse_args()

    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false" if args.no_content else "true")
    load_eval_runner_secrets()          # API keys from OpenBao into this process only
    telemetry = setup_telemetry(environment="development")
    genai.instrument()

    question = " ".join(args.question)
    session = args.session or f"cli-{uuid.uuid4().hex[:8]}"
    try:
        with genai.agent_span(agent_name="ec2-inventory-agent", question=question, model=agent_model(),
                              conversation_id=session, user_id=args.user,
                              extra={"agent.variant": args.variant}) as span:
            run = await run_agent(args.variant, question, thread_id=session)
            genai.set_agent_output(span, run.answer)
            trace_id = format(span.get_span_context().trace_id, "032x")
        print(run.stdout, end="")
        print("\n" + run.answer)
        print(f"\ntrace: {LANGFUSE_HOST}/project/{LANGFUSE_PROJECT}/traces/{trace_id}")
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
