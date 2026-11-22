# 06. SLOs and Burn-Rate Alerts

*Operating Agents Lab, article 6 of 8*

"Alert when the error rate is above 1%" pages people at 3 a.m. for a blip, and stays quiet through
a slow leak. Service level objectives give alerts a better question: **at this rate, will we use
up the month's error budget too soon?**

## The SLOs

| SLO | Good request | Target |
|---|---|---|
| availability | got an answer, or a deliberate budget refusal | 99.5% |
| full_quality | answered by a model, not the degraded baseline | 99.0% |
| latency | answered within 10 s | 95.0% |
| cost | at most $0.05 | 99.0% |

An error budget is `1 - target`: 0.5% of requests may fail availability. The **burn rate** is how
fast you're spending it. A burn rate of 1 uses exactly the budget over the SLO period; 14.4 uses
2% of a 30-day budget in an hour.

## Two windows per alert

| Severity | Burn rate | Long window | Short window |
|---|---|---|---|
| page | 14.4× | 1 h | 5 min |
| ticket | 6× | 6 h | 30 min |

An alert fires only when **both** windows exceed the rate. The long window proves it's real; the
short window proves it's still happening. This scheme is from Google's SRE workbook. The tests
check both halves: a sustained outage pages, and an outage that ended ten minutes ago doesn't.

## Spend alerts

Two cost alerts that SLOs don't cover:

- a **tenant past 80%** of its daily budget, as a ticket;
- a **spend spike**, where the last hour costs more than 3× the hourly average of the previous day,
  as a page. It needs at least an hour of history first. The first version of this lab didn't
  check that, and the game day caught it raising false spikes in the first hour of traffic.

## One source for tests and production

`slo.prometheus_rules()` renders the same SLOs and windows as a `PrometheusRule`.
`deploy/prometheus-rules.yaml` is generated from it, and a test fails if the committed file drifts.
Every rule links a runbook, and the test checks that each linked file exists. The metric names
assume the usual OpenTelemetry-to-Prometheus translation; verify them against your exporter.
