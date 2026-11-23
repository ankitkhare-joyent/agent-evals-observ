# 01 · Why agents need evals and observability

Data engineers already know how to make a Spark job trustworthy: unit tests on transformations, data-quality
checks (row counts, null rates, Great Expectations / dbt tests), lineage, and metrics on every run.
An agent needs the same discipline, but three things make it harder.

## What is different about agents

| Batch pipeline | Agent |
|---|---|
| Same input gives the same output | Same question can take a different path and give a different answer |
| The code decides the control flow | **The model** decides which tool to call, with what arguments, and when to stop |
| A wrong result is usually a crash or a failed check | A wrong result is a fluent, confident, wrong sentence |
| Cost is roughly fixed per run | Cost depends on how many loops the model takes (tokens × steps) |

So two questions you never had to ask about a Spark job become central:

1. **What did it actually do?** Which tools, which arguments, what came back, how many model calls,
   how many tokens, how long. That is **observability**: traces of every run.
2. **Is it good, and is it still good after my change?** That is **evaluation**: a repeatable,
   scored test suite, plus scoring of real production traffic.

## The loop production teams run

```
          ┌────────────── offline evals (before you ship) ──────────────┐
          │  golden dataset ──► run agent ──► graders ──► report + CI gate │
          └───────────────▲───────────────────────────────┬───────────┘
                          │ new failure cases                 │ ship
          ┌───────────────┴───────────────────────────────▼───────────┐
          │  production traffic ──► traces ──► online evals + metrics   │
          │                       (Langfuse)    (sampled judge, SLOs)   │
          └──────────────── observability (after you ship) ─────────────┘
```

Production failures you find in traces become new eval cases. Evals stop the same failure from
coming back. That flywheel is the whole game; every lesson here is one piece of it.

## Vocabulary (used everywhere after this)

- **Trace**: one end-to-end run (one user question). Made of **spans**.
- **Span**: one timed operation with attributes: `invoke_agent`, `chat claude-opus-5-5`,
  `tools/call find_instances`. Spans nest (parent/child).
- **Trajectory**: the ordered list of model calls and tool calls an agent made. Read from the trace.
- **Eval case**: an input plus what a correct answer must contain or must not do.
- **Grader**: a function that scores one property of one run. *Code graders* (regex, exact match,
  "every instance ID came from a tool result") or *model graders* (an **LLM judge** with a rubric).
- **Experiment / eval run**: the whole dataset run once through one version of the agent.
- **Offline eval**: before deploy, on a fixed dataset. **Online eval**: after deploy, on sampled
  production traces, usually without a reference answer.
- **pass@k / pass^k**: did the case pass *at least once* in k tries (capability), versus
  *every time* (reliability). Users feel pass^k.

## What you will build here

Everything runs locally against the agent from `../learn-ai-agent`, without changing a line of it:

1. **OpenTelemetry instrumentation** that follows the GenAI semantic conventions (lessons 02-03).
2. An **OpenTelemetry Collector** that redacts secrets, samples, derives metrics and routes data (04).
3. **Langfuse**, the open source LLM observability and eval platform, as the trace store and UI (05).
4. An **offline eval suite**: generated golden cases, code graders, an LLM judge, reliability stats,
   reports and a CI gate (06-08).
5. **Online evals and SLO alerts** on production traces (09).
6. **OpenBao** holding every secret, with sidecars delivering them (10).

Next: [02 · OpenTelemetry and the GenAI conventions](02-opentelemetry-genai.md)
