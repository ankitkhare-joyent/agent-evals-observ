# 12 · Architecture

The whole system on one page. GitHub renders the Mermaid diagram below; numbers match the lessons.

```mermaid
flowchart LR
  claude["Claude API<br/>agent: Opus 5.5 · judge: Sonnet 5.5"]

  subgraph AGENT["Agent under test (../learn-ai-agent, unchanged)"]
    direction TB
    agent["EC2 inventory agent<br/>raw loop | LangGraph"]
    mcp["MCP server :8001<br/>4 read-only tools"]
    api["REST API :8000<br/>FastAPI + DuckDB"]
    lake[("Iceberg lakehouse<br/>Lakekeeper + SeaweedFS")]
    lbao["Lakehouse OpenBao :8200<br/>agent's own login"]
    agent -->|MCP over HTTP<br/>traceparent in _meta| mcp --> api --> lake
    agent -.->|agent login| lbao
  end

  subgraph OURS["This project (host processes)"]
    direction TB
    instr["① GenAI instrumentation<br/>wrapt patches anthropic + mcp<br/>invoke_agent › chat · tools/call"]
    runner["② Offline eval runner<br/>oracle → golden cases → k reps<br/>code graders + LLM judge → CI gate"]
    online["③ Online evaluator<br/>sampled real traces, no reference"]
  end

  subgraph COL["④ OTel Collector :34318"]
    direction TB
    recv["otlp receiver"] --> ml["memory_limiter"] --> red["redaction"] --> cap["cap sizes"]
    cap --> sm["spanmetrics<br/>(unsampled RED)"] --> prom[("Prometheus :39095<br/>SLO alerts")]
    cap --> ts["tail_sampling"] --> exp["otlphttp<br/>Basic auth from file"]
  end

  subgraph LF["⑤ Langfuse v4 :33000"]
    direction TB
    web["langfuse-web<br/>UI · API · OTLP ingest"]
    s3[("MinIO<br/>raw events")]
    redis[("Redis<br/>queue")]
    worker["langfuse-worker"]
    ch[("ClickHouse<br/>traces · scores")]
    pg[("Postgres<br/>datasets · keys")]
    web --> s3 --> worker
    web --> redis --> worker
    worker --> ch
    web --> pg
  end

  subgraph BAO["⑥ OpenBao :38200 (MPL-2.0 Vault fork)"]
    direction TB
    bao["server: Raft · static-key auto-unseal<br/>self-init · audit · KV v2 · AppRole ×3<br/>database engine (dynamic Postgres users)"]
    sc1["bao-agent-collector<br/>sidecar → tmpfs"]
    sc2["bao-agent-langfuse<br/>sidecar → tmpfs"]
    bao --> sc1
    bao --> sc2
  end

  instr -. patches .-> agent
  runner -->|runs| agent
  agent --> claude
  runner -->|judge| claude
  online -->|judge| claude
  instr ==>|OTLP| recv
  exp ==>|OTLP| web
  runner -->|datasets · scores| web
  online -->|read traces · write scores| web
  bao -. AppRole: keys into process env .-> runner
  sc1 -. Authorization header file .-> exp
  sc2 -. env + password files .-> web
```

The same system as a hand-drawn diagram, with more detail per box: [architecture.html](architecture.html)
(download it and open in a browser; GitHub shows HTML files as source).

## Follow one question through the system

1. `mise run trace -- "Which account has the most running vCPUs?"` starts a Python process. It logs into
   **OpenBao** with the `eval-runner` AppRole, puts the Anthropic key in its own environment, and
   revokes its token.
2. It installs the OTel SDK and the **GenAI instrumentation** (lesson 03), then calls the agent's own
   `ask()`. The agent fetches its inventory login from the *lakehouse's* OpenBao, as it always does.
3. The agent loops: a `chat` call to Claude, an MCP `tools/call` (with `traceparent` in `_meta`) to the
   MCP server, which calls the REST API, which reads the DuckDB hot cache over Iceberg; then Claude again.
   Each step is a span under one `invoke_agent` root.
4. Spans leave in batches over **OTLP** to the **Collector** (lesson 04): secrets masked, sizes capped,
   RED metrics counted for **Prometheus**, then tail-sampled and sent to **Langfuse** with a key that a
   sidecar rendered from OpenBao into memory.
5. **Langfuse** (lesson 05) writes the raw events to MinIO, queues them in Redis, and the worker builds
   observations with costs in **ClickHouse**. You see the trace in the UI.
6. Later, the **online evaluator** (lesson 09) samples that trace through the public API, judges it
   without a reference, and writes `online_*` scores back. A low score becomes a new golden case.
7. Before the next change ships, the **offline runner** (lesson 07) builds golden cases from live data,
   runs the agent k times per case, grades each run from its own spans plus an LLM judge, files the runs
   in Langfuse as an experiment on the dataset, and fails CI if a gate is missed.

## Where each piece lives

| Box in the diagram | Code / config |
|---|---|
| GenAI instrumentation | [observ/genai.py](../observ/genai.py), [observ/telemetry.py](../observ/telemetry.py) |
| Agent adapter (read-only import) | [observ/agent_under_test.py](../observ/agent_under_test.py) |
| Offline eval runner | [evals/run.py](../evals/run.py), [evals/cases.py](../evals/cases.py), [evals/graders/](../evals/graders/), [evals/report.py](../evals/report.py) |
| Online evaluator, judge calibration | [evals/online.py](../evals/online.py), [evals/calibrate_judge.py](../evals/calibrate_judge.py) |
| Collector, Prometheus | [stack/otel-collector/config.yaml](../stack/otel-collector/config.yaml), [stack/prometheus/](../stack/prometheus/) |
| Langfuse stack | [compose.yaml](../compose.yaml), [stack/clickhouse/low-resources.xml](../stack/clickhouse/low-resources.xml) |
| OpenBao server, policies, sidecars | [stack/openbao/](../stack/openbao/), [scripts/bao_seed.py](../scripts/bao_seed.py), [observ/secrets.py](../observ/secrets.py) |
| CI gate | [.github/workflows/evals.yml](../.github/workflows/evals.yml) |
