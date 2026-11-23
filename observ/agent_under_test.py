"""Adapter for the agent we evaluate: the EC2-inventory agent in ../learn-ai-agent (read-only).

We call its public entry points exactly as its own CLI does, so the eval measures the code that ships
(an eval that re-implements the agent measures something else). Two variants exist there:

    raw_loop   - Claude API + MCP client + a while-loop (agents/raw_loop.py)
    langgraph  - the same agent as a LangGraph state machine (agents/langgraph_agent.py)

The agent reads ANTHROPIC_API_KEY and AGENT_MODEL from the environment (observ.secrets puts the key there
from this project's OpenBao). Its login to the inventory API it fetches itself, from the lakehouse
project's own OpenBao (localhost:8200) - each system owns its own secrets.
"""
from __future__ import annotations

import contextvars
import io
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

AGENT_SRC = Path(os.environ.get("AGENT_SRC", Path(__file__).resolve().parents[2] / "learn-ai-agent")).resolve()
VARIANTS = ("raw_loop", "langgraph")


def _import_path() -> None:
    if not (AGENT_SRC / "agents" / "common.py").exists():
        raise SystemExit(f"agent source not found at {AGENT_SRC} (set AGENT_SRC)")
    if str(AGENT_SRC) not in sys.path:
        sys.path.insert(0, str(AGENT_SRC))


def agent_model() -> str:
    _import_path()
    from agents.common import MODEL
    return MODEL


class _TaskStdout(io.TextIOBase):
    """sys.stdout replacement that sends each asyncio task's prints to that task's own buffer.

    contextlib.redirect_stdout swaps one *global*; with several agent runs in flight, their prints (and the
    runner's) would land in each other's buffers. A ContextVar is per-task, so this routes correctly.
    """

    def __init__(self, real):
        self.real = real
        self.buffer_var: contextvars.ContextVar[io.StringIO | None] = contextvars.ContextVar("agent_stdout",
                                                                                          default=None)

    def write(self, s: str) -> int:
        return (self.buffer_var.get() or self.real).write(s)

    def flush(self) -> None:
        self.real.flush()


def _router() -> _TaskStdout:
    if not isinstance(sys.stdout, _TaskStdout):
        sys.stdout = _TaskStdout(sys.stdout)
    return sys.stdout


@dataclass
class AgentRun:
    answer: str
    stdout: str          # what the agent printed (its own step log), kept for debugging transcripts


async def run_agent(variant: str, question: str, *, thread_id: str | None = None) -> AgentRun:
    """One question in, one answer out. Exceptions propagate (the caller decides if it's an infra error)."""
    _import_path()
    router, buf = _router(), io.StringIO()
    token = router.buffer_var.set(buf)
    try:
        if variant == "raw_loop":
            from agents.raw_loop import ask
            answer = await ask(question)
        elif variant == "langgraph":
            from agents.langgraph_agent import build_graph, load_tools, make_model, run_turn
            app = build_graph(make_model(), await load_tools())
            answer = await run_turn(app, question, thread_id or str(uuid.uuid4()))
        else:
            raise ValueError(f"unknown agent variant {variant!r}; expected one of {VARIANTS}")
    finally:
        router.buffer_var.reset(token)
    return AgentRun(answer=answer, stdout=buf.getvalue())
