# Lab 05 · One agent run, as spans

One sitting, about 40 minutes. Background: [docs/02](../docs/02-opentelemetry-genai.md),
[docs/03](../docs/03-instrumenting-the-agent.md). Code: [observ/genai.py](../observ/genai.py),
[observ/telemetry.py](../observ/telemetry.py), [observ/run_traced.py](../observ/run_traced.py).

Labs 03 and 04 used hand-made spans. Now the real thing: the EC2 agent from `../learn-ai-agent`, unchanged,
instrumented from the outside. **This lab calls Claude**: five agent runs, about 10 cents in total. After it you can
explain:

- what spans one question produces, and which attribute each Langfuse column comes from
- what prompt caching does to tokens and cost, and why the two agent variants cost different amounts
- what turning content capture off removes
- why an agent can fail with an API error when the real problem is in your shell

## Before you start

```bash
cd ~/workstuff/agent-evals-observ
source lab/env.sh
curl -s localhost:8000/readyz | jq -c '{ready, rows}'     # the lakehouse API: ready, ~1000 rows in the hot cache
```

## 1. The instrumentation, without the network

[tests/test_instrumentation.py](../tests/test_instrumentation.py) points the Anthropic SDK at a fake HTTP server
and checks the spans in memory. Read the first test before running it.

```bash
mise exec -- uv run pytest -q tests/test_instrumentation.py
```

## 2. One question

**Predict (P1):** for "Which account has the most running vCPUs right now?", how many `chat` (GENERATION) spans and
how many tool spans will the trace have, and which tool?

```bash
mise run -q trace -- "Which account has the most running vCPUs right now?" | tail -3
sleep 18; lf_trace $(lf_last)
```

The columns are type, name, model, input tokens, output tokens, cost in USD.

**Predict (P2):** the second `chat` call re-sends the whole conversation, which is longer than the first. Why does it
report only 2 input tokens? Look at the full usage:

```bash
T=$(lf_last)
lf_get api/public/v2/observations "traceId=$T&fields=core,basic,usage" | jq -c '.data[] | select(.type=="GENERATION") | .usageDetails'
lf_get api/public/v2/observations "traceId=$T&fields=core,basic,io" | jq -r '.data[] | select(.type=="TOOL") | "input:  \(.input)\noutput: \(.output[0:120])..."'
lf_get api/public/v2/observations "traceId=$T&fields=core,basic" | jq -c '.data[] | select(.type=="AGENT") | {sessionId, userId, environment}'
```

Find where each of `sessionId`, `userId` and `environment` was set: `agent_span()` in
[observ/genai.py](../observ/genai.py) and `setup_telemetry()` in [observ/telemetry.py](../observ/telemetry.py).

## 3. The same question, with content capture off

**Predict (P3):** with `--no-content`, what will Langfuse still show, and what will be missing? Will this run cost
the same as the first?

```bash
mise run -q trace -- --no-content "Which account has the most running vCPUs right now?" | tail -1
sleep 18; T=$(lf_last); lf_trace $T
lf_get api/public/v2/observations "traceId=$T&fields=core,basic,io" | jq -c '.data[] | {type, input: (.input // "" | tostring | .[0:30])}'
```

## 4. The other implementation

**Predict (P4):** the LangGraph variant runs the same model, prompt and tools through LangChain. Same spans? Same
cost?

```bash
mise run -q trace -- --variant langgraph "Which account has the most running vCPUs right now?" | tail -1
sleep 18; T=$(lf_last); lf_trace $T
lf_get api/public/v2/observations "traceId=$T&fields=core,basic,usage" | jq -c '.data[] | select(.type=="GENERATION") | .usageDetails'
```

## Break it, then fix it

**Break 1: two OpenBaos, one variable name.** The agent under test finds *its* OpenBao through `BAO_ADDR` and
`BAO_TOKEN` (see `agents/secrets.py` in learn-ai-agent). That's why `lab/env.sh` uses `EVALS_BAO_ADDR` and
`EVALS_BAO_TOKEN` for this project's. Run the agent as if `env.sh` had used the common names:

```bash
BAO_ADDR=$EVALS_BAO_ADDR BAO_TOKEN=$EVALS_BAO_TOKEN mise run -q trace -- "Which account has the most running vCPUs right now?" \
  2>&1 | grep -E '^httpx' | tail -1
sleep 18; lf_get api/public/v2/observations "type=AGENT&fields=core,basic&limit=1" | jq -c '.data[0] | {level, statusMessage: (.statusMessage | split("\n")[0])}'
```

**Predict (P5):** what does the error name as the problem, and what is the real one? What does the trace show?

**Fix:** nothing to repair: the override only lived in that one command. In your own shells, never export another
system's variable names (`BAO_ADDR`, `VAULT_ADDR`, `AWS_PROFILE`, ...) globally; scope them per project (mise's
`[env]` does this per directory).

**Break 2: the Collector is down.** Stop it, then run a hand-made span and a real question:

```bash
docker compose stop otel-collector
time (mise run -q lab:span; echo "exit=$?")
mise run -q trace -- "How many instances are running in eu-west-1 right now?" 2>&1 | tail -4
```

**Predict (P6):** does the agent fail? Does anything tell you the trace was lost? (Why not simply set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:1` in front of `mise run`? Try `OTEL_EXPORTER_OTLP_ENDPOINT=x mise exec
-- printenv OTEL_EXPORTER_OTLP_ENDPOINT` and see.)

**Fix:** start it again. The two traces from the outage never arrive:

```bash
docker compose start otel-collector; sleep 25
lf_get api/public/v2/observations "type=AGENT&fields=core,basic&limit=3" | jq -r '.data[] | "\(.startTime) \(.name)"'
```

## Check your predictions

**P1.** Two `chat` spans and one tool span, `fleet_summary`:

```
AGENT       invoke_agent ec2-inventory-agent
GENERATION  chat claude-opus-5-5   claude-opus-5-5  4  75   0.011896
TOOL        fleet_summary
GENERATION  chat claude-opus-5-5   claude-opus-5-5  2  347  0.0096682
```

The first model call decides to call `fleet_summary`, the second reads the result and answers. The answer named
account `392918273645` (2,654 running vCPUs across 284 instances, snapshot 2026-09-30 22:00 UTC).

**P2.** Prompt caching. The full usage:

```
{"input":4,"output":75,"input_cached_tokens":0,"input_cache_creation":2076,"total":2155}
{"input":2,"output":347,"input_cached_tokens":2076,"input_cache_creation":461,"total":2886}
```

The raw loop marks the prompt for caching (`cache_control` in `agents/raw_loop.py`). Call 1 *wrote* 2,076 tokens
(system prompt + tool definitions + question) to the cache at 1.25× the input price; call 2 *read* those 2,076 at
0.05× and wrote the new 461. Only 2-4 tokens are billed at the full rate. That's why call 1 costs more than call 2
despite being shorter. The tool span shows `input: {"group_by": "account_id", "state": "running"}` and the JSON
result. The root has `{"sessionId":"cli-a767a721","userId":"ankit","environment":"development"}`: `session.id` and
`user.id` come from baggage set in `agent_span()`, `environment` from the resource attribute
`deployment.environment.name`.

**P3.** The same tree, the same model, tokens and costs, but every input and output is empty
(`{"type":"GENERATION","input":""}` and so on). Cheaper, too: `0.0019312` for the first call instead of `0.011896`,
because the cache written by the run in step 2 was still warm (it lives 5 minutes), so this time call 1 *read*
2,076 tokens instead of writing them. Content capture changes what you can debug, not what the run costs.

**P4.** Same tree (`chat` → `fleet_summary` → `chat`), because the patch is on the Anthropic SDK and MCP client
that LangChain uses underneath. Not the same cost: `0.009024` + `0.017468`, about twice the raw loop's, because
nothing is cached:

```
{"input":1881,"output":75,"input_cached_tokens":0,"input_cache_creation":0,"total":1956}
{"input":2442,"output":385,"input_cached_tokens":0,"input_cache_creation":0,"total":2827}
```

`ChatAnthropic` isn't given a cache breakpoint in `agents/langgraph_agent.py`. The input is also ~200 tokens smaller
(1,881 vs 2,080): LangChain converts the MCP tool schemas into slightly different JSON. Same agent on paper,
different bill, and the trace is how you'd find out.

**P5.** `httpx.HTTPStatusError: Client error '422 Unprocessable Content' for url 'http://localhost:8000/auth/token'`.
The error blames the **lakehouse API** with a 422. The real cause is that the agent asked *this* project's OpenBao
for `ec2-lakehouse/agent`, got a 404, silently fell back to an empty password, and the API rejected the empty form
field. The trace shows only the root, at `level: ERROR` with that message: the run died before the first model
call, so no `chat` span exists. (I hit this for real while writing the lab, which is why the variables are
`EVALS_*`.)

**P6.** The agent doesn't fail: it answers, prints its trace link, and exits 0. The exporter retries in the
background and gives up at shutdown, printing warnings to stderr:

```
Transient error HTTPConnectionPool(host='127.0.0.1', port=34318): ... Connection refused ... retrying in 1.19s.
Transient error ... retrying in 1.94s.
Transient error ... retrying in 3.28s.
Failed to export spans batch due to timeout, max retries or shutdown.
```

`lab:span` took 7 seconds instead of half a second (the retries happen during `shutdown()`), and still exited 0.
The trace link points at a trace that will never exist: after restarting the Collector, the newest traces are from
before the outage. This is deliberate, since telemetry must never break the app, but nobody reads a CLI's stderr in
production. Alert from the *receiving* side: "no spans from service X for 10 minutes" in Prometheus, plus the
Collector's own `refused` / `send_failed` counters.

And the aside: mise's `[env]` wins over your shell, so `OTEL_EXPORTER_OTLP_ENDPOINT=x mise run …` still prints the
mise value. My first attempt at this break did exactly that, the traces went through, and I almost recorded the
wrong answer. Check what a process actually sees, not what you exported.

## What to remember

- One question = `invoke_agent` root + one `chat` per model call + one `tools/call` per tool call.
- Cache writes cost more than plain input, cache reads far less. `input_cache_creation` vs `input_cached_tokens`
  tells you which happened.
- Two implementations of "the same agent" can differ 2× in cost; only the trace makes it visible.
- Telemetry failures never fail the app; at most a warning on stderr. Alert on missing data, not on errors.

Next: [Lab 06 · Metrics](06-metrics.md).
