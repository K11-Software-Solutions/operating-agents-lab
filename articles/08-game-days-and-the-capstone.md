# 08. Game Days, Runbooks, and the Capstone

*Operating Agents Lab, article 8 of 8*

Resilience code that has never seen a failure is a hope. A game day breaks things on purpose and
checks that the system, and the runbook, respond as designed.

## Four scenarios

`python -m opsagents.gameday` replays traffic on a simulated clock (one request every 20 s, hours
in a second) through the real agent, resilience code and alert rules, injecting faults at set
times:

| Scenario | Injected | Expected |
|---|---|---|
| provider-outage | primary down 30 min, then both down 10 min | fallback, then labelled degraded answers; no errors; full-quality page and ticket; the breaker spares the primary |
| runaway-output | 50,000-token answers from minute 100 | cost page and ticket; spend-spike page; the daily budget stops the spend |
| noisy-tenant | one tenant at 10× traffic on a small budget | that tenant cut off and ticketed; the other untouched |
| rate-limit-storm | 429 with `Retry-After` on every first attempt for 20 min | retries absorb it; no alerts |

Each compares **exactly** which alerts fired with which should have. That makes the game day
strict about false alarms too: the first run found two (a spend spike computed from too little
history), and the fix is in `slo.py`. A negative control claims an outage is silent, and the game
day must report a mismatch.

## Runbooks

Every alert links a runbook in `runbooks/`. Each one starts with **what the system already did**:
retried, opened the breaker, fell back, refused the tenant. The person paged then knows what's
left for them. The game day is how you know those sentences are true.

## What the series built

| Lab | Built |
|---|---|
| 1 | model calls, structured outputs, tools, an agent loop, an eval gate |
| 2 | retrieval with citations and two-layer evals |
| 3 | one agent in four frameworks, on one eval |
| 4 | an MCP server with auth and a contract gate |
| 5 | team workflows for Claude Code, Cursor and Copilot |
| 6 | four PDLC agents, each with an objective eval and a rules baseline |
| 7 | a hardened service, a sandbox, release gates, shadow and canary |
| 8 | EKS, IRSA, Helm, and infrastructure tests without an AWS account |
| 9 | traces, metrics, resilience, budgets, SLOs, scheduled agents, game days |

## Next: the capstone

The capstone puts it together: the Kestrel PDLC platform on EKS, released with an assurance case.
The assurance case is the argument, built from the evidence the labs produce, that it's fit to
ship: eval results, gate decisions, isolation probes, game-day reports. It uses the method from
the AI Quality Engineering series'
[AI Assurance Case Lab](https://github.com/K11-Software-Solutions/ai-assurance-case-lab).
