# 01. What Operating an Agent Means

*Operating Agents Lab, article 1 of 8*

Labs 7 and 8 shipped and deployed the agents. Operating them is what happens afterwards, every
day: knowing whether they're working, noticing when they aren't, and limiting the damage when
something goes wrong while nobody is looking.

## Four things to watch

| Signal | Ordinary services | Agents add |
|---|---|---|
| Availability | did the request succeed? | did it get a *real* answer, or a degraded one? |
| Latency | how long? | how many model calls and tool calls, and which was slow? |
| Cost | mostly fixed | variable per request: a prompt change can double it overnight |
| Quality | — | is the answer still good? Covered by evals, not by status codes |

## Agents fail in their own ways

- **The provider has a bad day.** Rate limits, overload, outages. Your code is fine; your
  dependency isn't.
- **A loop runs away.** The agent keeps calling tools, or the model writes a 50,000-token answer.
- **One tenant uses everyone's budget.**
- **Quality drifts quietly.** No errors, slightly worse answers.

## This lab's answer to each

| Failure | Detected by | Contained by |
|---|---|---|
| provider trouble | full-quality and availability burn-rate alerts | retry, circuit breaker, fallback model, degraded mode |
| runaway cost | cost burn and spend-spike alerts | request budget, tenant daily budget |
| noisy tenant | tenant-budget ticket | per-tenant daily budget |
| quality drift | nightly eval-drift job | rollback (lab 7) |

The game day (article 08) runs each failure on purpose and checks both columns.
