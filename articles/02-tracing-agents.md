# 02. Tracing Agents with OpenTelemetry

*Operating Agents Lab, article 2 of 8*

A slow or expensive agent request is a tree of work: model calls, tool calls, retries. A trace
shows that tree.

```text
invoke_agent triage                  kestrel.outcome=ok, kestrel.cost_usd=0.006, kestrel.tenant=acme
├─ chat claude-opus-5-5              gen_ai.usage.input_tokens=320, output_tokens=40, finish_reasons=[tool_use]
│    event: retry (attempt 1, RateLimited, delay 0.01 s)
├─ execute_tool search_backlog       gen_ai.tool.name=search_backlog
└─ chat claude-opus-5-5              gen_ai.usage.input_tokens=393, output_tokens=120, finish_reasons=[end_turn]
```

## Use the GenAI semantic conventions

OpenTelemetry defines standard attribute names for AI calls: `gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.tool.name` and more. The
names in this lab were checked against the installed `opentelemetry-semantic-conventions` package.
Standard names mean any backend that understands them can show token usage and model latency
without custom dashboards.

The conventions are still marked as in development, so names can change. Pin the package, and
treat an upgrade like any other dependency change.

## What goes on which span

- **The root span:** tenant, request id, outcome, total cost and number of model calls. That's
  enough to find the expensive or failing requests.
- **The chat spans:** model, tokens, finish reason and cost. Retries are span events with the error
  type and delay, so a slow request explains itself.
- **The tool spans:** the tool name. Arguments only if they're safe to store.

## What doesn't go in a trace

**Prompts and answers stay out by default.** They can contain customer data, code or secrets, and
traces are often kept longer and read more widely than application data. The conventions provide
opt-in attributes for message content; turn them on only where the retention and access controls
fit. A test checks that the request text never reaches the log line.

## In tests and in production

`setup()` uses in-memory exporters, so tests can assert on the span tree: names, parents,
attributes and events. In production, swap in OTLP exporters pointing at a collector. The
instrumentation code doesn't change.
