# 05. Budgets That Stop Runaway Spend

*Operating Agents Lab, article 5 of 8*

An agent's cost per request is decided at run time, by the model. Most of the time it's small.
Sometimes it isn't: a loop that keeps calling tools, an answer that never ends, a prompt change
that makes every answer three times longer. Budgets turn "sometimes" into a bounded number.

## Per request

```python
RequestBudget(max_model_calls=6, max_tokens=40_000, max_cost_usd=0.25)
```

Before each model call, the agent checks the budget. When any limit is reached, the request stops
with `budget_exceeded`. Add `max_tokens` on every API call as well; it's the only limit that
applies *within* a single reply.

## Per tenant, per day

```python
TenantBudgets(daily_usd={"acme": 50.0}, default_daily_usd=5.0)
```

Every request's cost, including attempts that failed part-way, is charged to its tenant. A tenant
past its budget is refused with HTTP 429 until the next day. In the noisy-tenant game day, the
noisy tenant is cut off and ticketed, and the quiet tenant doesn't notice.

## The last line of defence

In the runaway-output game day, the model starts writing 50,000-token answers, at about $1 each.
The spend-spike alert pages within the hour. Even if nobody acts, the tenant's daily budget stops
the spend once it's used up. Alerts tell a person; budgets work when no person is there.

## Refusing is not an outage

A tenant over budget gets a deliberate 429. That's a policy doing its job, so `budget_exceeded`
doesn't count against the availability SLO. A test checks this, because otherwise a single noisy
tenant could page the on-call engineer for something working as designed.

## Setting the numbers

Start from data: the p99 cost per request on your eval set, times a margin, for the request
budget; and each tenant's expected daily volume, times a margin, for theirs. Review them when
models or prompts change, because those change cost.
