# Runbook: latency or cost per request out of SLO

**Alerts:** `latency_burn`, `cost_burn`.

1. Check whether the share of `fallback` and `degraded` outcomes changed. Retries and fallbacks
   add latency.
2. Compare p95 `gen_ai.client.operation.duration` by model with `kestrel.agent.request.duration`.
   If the model is slow, it's the provider (see provider-outage.md). If the request is slow but the
   model isn't, it's our tools or our queueing.
3. For cost, see cost-spike.md.
4. If a recent deploy lines up with the change, roll it back first and investigate after.
