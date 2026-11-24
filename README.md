# mcp-tool-agent

IT equipment request handler for BEEHIVE Cloud, Inc. (fictional): an MCP server that exposes the policy
as four tools, and a ReAct agent that uses them to approve, deny or escalate a request.

Spec and policy rules: [docs/requirements.md](docs/requirements.md).

## Layout

| Path | What it is |
|------|------------|
| `src/equipment/tools.py` | The four tools as plain functions, including the rule engine (rules 1-14) |
| `src/equipment/server.py` | MCP server: thin wrappers over `tools.py` |
| `src/equipment/agent.py` | ReAct loop, deterministic verifier, reflection, bounded revision |
| `src/equipment/tracing.py` | OpenTelemetry exporter setup |
| `scripts/smoke_client.py` | Minimal MCP client: connect, list tools, call one |
| `scripts/demo.py` | Runs sample requests through the live agent |
| `data/` | Synthetic employees, catalog, policy and 21 sample requests with expected outcomes |

## Run

```sh
uv sync
uv run pytest                                   # unit + integration tests, 90% branch-coverage gate
uv run python scripts/smoke_client.py           # server/client connectivity, no model needed

cp .env.example .env                            # then add ANTHROPIC_API_KEY
uv run --env-file .env python scripts/demo.py   # four live requests with full traces
uv run --env-file .env python -m equipment.agent BC-2105 "Could I get a 27-inch 4K monitor?"
```

## How a request is handled

1. **ReAct loop.** The model calls the MCP tools; each call is recorded as Thought / Action / Observation.
2. **Draft.** The model hands in a decision and a reply.
3. **Verifier (code).** The decision must match what `check_request_eligibility` returned, every date and
   dollar amount in the reply must appear in a tool result, and an escalation must have exactly one ticket.
4. **Reflection (model).** A second call checks the reply promises nothing the tool results don't support.
5. **Revise.** A failed draft goes back to the model, at most twice. After that the code escalates itself.

## Tracing

The MCP SDK emits a span for every request and carries the trace ID from agent to server, so one request
is one trace across both processes. Choose where spans go:

```sh
EQUIPMENT_TRACE=console uv run --env-file .env python scripts/demo.py REQ-5001          # one line per span
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 uv run --env-file .env python scripts/demo.py   # Jaeger etc.
```

LangSmith (hosted, built for LLM runs): add to `.env`, then run the demo as usual.

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=https://api.smith.langchain.com/otel
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=<langsmith key>,Langsmith-Project=mcp-tool-agent
ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE=content   # include prompts and responses in model spans
```

Jaeger with a browser UI at http://localhost:16686:

```sh
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one
```
