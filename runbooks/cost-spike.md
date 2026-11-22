# Runbook: spend spike

**Alerts:** `spend_spike` (page), `cost_burn` (page or ticket), `tenant_budget` (ticket).

## What the system already did

- The **per-request budget** stopped requests at their model-call, token or cost limit.
- **Tenant daily budgets** refused further work for any tenant who ran out (outcome
  `budget_exceeded`, HTTP 429). Other tenants were unaffected.

## What you do

1. **Find the source.** Break `kestrel_agent_cost_usd_total` down by tenant, route and variant.
   One tenant? One variant (a new prompt or model in canary)? Everyone?
2. **Long outputs?** Compare `gen_ai.client.token.usage` with `gen_ai.token.type="output"` against
   last week. A prompt change that makes the model "explain more" can multiply cost overnight.
3. **A loop?** Look for `invoke_agent` spans with many `chat` children. Each one should end within
   `max_model_calls`.
4. **Act.** Roll back the variant (lab 7's promotion gate), lower that tenant's budget, or cap
   `max_tokens`. Cut the spend first, then investigate.
