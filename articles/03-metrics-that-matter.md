# 03. Metrics That Matter

*Operating Agents Lab, article 3 of 8*

Traces explain one request. Metrics show all of them, cheaply, and they're what alerts are built
on.

| Metric | Type | Labels | Answers |
|---|---|---|---|
| `gen_ai.client.token.usage` | counter | token type, model, route, variant | where are tokens going? |
| `gen_ai.client.operation.duration` | histogram (s) | model, route, variant | is the model slow? |
| `kestrel.agent.request.duration` | histogram (s, with a 10 s bucket) | route | is the request slow? |
| `kestrel.agent.cost_usd` | counter | route, variant, tenant | who is spending what? |
| `kestrel.agent.requests` | counter | route, outcome | how many got real answers? |
| `kestrel.agent.expensive_requests` | counter | route | how many broke the cost SLO? |

The first two are standard GenAI metrics. The rest are this service's own.

## Labels by variant

Lab 7's canary and shadow variants are labels here. "Did the candidate get more expensive?" is
then a query, not an investigation.

## Mind the cardinality

Each distinct combination of label values is a separate time series. Route, variant, outcome and
model are a handful of values each, so they're safe. Tenant is safe only when tenants number in
the hundreds, not millions. Never use request ids, user ids or prompt text as labels. Those
belong in traces and logs.

## Why both model and request latency

If `gen_ai.client.operation.duration` is fine but the request is slow, the time is going
somewhere else: our tools, retries, queueing. If both are slow, it's the provider. The latency
runbook starts with exactly that comparison.

## Cost is a first-class metric

Counting dollars directly, not just tokens, means a model change shows up as a cost change, and a
spend alert can't be fooled by a cheaper model with longer answers, or a pricier one with shorter
ones.
