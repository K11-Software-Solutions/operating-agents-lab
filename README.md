# Operating Agents Lab

**Lab 9 of [Becoming an AI Engineer](https://github.com/K11-Software-Solutions/becoming-an-ai-engineer).**
Run agents in production and sleep at night. This lab covers:
- **OpenTelemetry** traces and metrics using the GenAI semantic conventions;
- **resilience** for a model provider's bad day: retries, a circuit breaker, a fallback model, and
  a labelled degraded mode;
- **budgets** that stop runaway spend, per request and per tenant;
- **SLO burn-rate alerts**, generated as Prometheus rules from the same definitions the tests use;
- **scheduled agents** that are safe to retry;
- **runbooks**, and a **game day** that breaks things on purpose and checks every response.

## The AI Engineering series

| # | Lab | Status |
|---|---|---|
| 1 | [AI Engineering Foundations](https://github.com/K11-Software-Solutions/becoming-an-ai-engineer) | ✅ |
| 2 | [RAG Engineering Lab](https://github.com/K11-Software-Solutions/rag-engineering-lab) | ✅ |
| 3 | [Agent SDK Lab](https://github.com/K11-Software-Solutions/agent-sdk-lab) | ✅ |
| 4 | [MCP Server Engineering Lab](https://github.com/K11-Software-Solutions/mcp-server-engineering-lab) | ✅ |
| 5 | [AI Coding Assistant Extensions Lab](https://github.com/K11-Software-Solutions/coding-assistant-extensions-lab) | ✅ |
| 6 | [PDLC Agents Lab](https://github.com/K11-Software-Solutions/pdlc-agents-lab) | ✅ |
| 7 | [Shipping Agents Lab](https://github.com/K11-Software-Solutions/shipping-agents-lab) | ✅ |
| 8 | [Agents on AWS Lab](https://github.com/K11-Software-Solutions/agents-on-aws-lab) | ✅ |
| 9 | **Operating Agents Lab** (this repo) | ✅ |
| — | Capstone | planned |

## One request, defended and observed

```text
tenant budget ─> agent loop ─> each model call: request budget ─> circuit breaker ─> retry (backoff, Retry-After)
                     │                          primary model ──fails──> fallback model ──fails──> rules baseline
                     └─> trace (invoke_agent ─ chat ─ execute_tool ─ chat), metrics, one JSON log event
```

| Outcome | Meaning | Counts against |
|---|---|---|
| `ok` | answered by the primary model | — |
| `fallback` | answered by the cheaper fallback model | — |
| `degraded` | both models unavailable; answered by the rules baseline, and labelled | full-quality SLO |
| `budget_exceeded` | the tenant's daily budget is used up (HTTP 429) | nothing: a policy working |
| `error` | no answer | availability SLO |

## Game day results

```text
PASS  provider-outage     fallback served for 30 min, degraded for 10; full-quality page and ticket fired;
                          no request failed; the circuit breaker spared the primary
PASS  runaway-output      cost page and ticket, plus a spend-spike page; the tenant's daily budget stopped the spend
PASS  noisy-tenant        the noisy tenant was cut off and ticketed; the quiet tenant was unaffected
PASS  rate-limit-storm    retries with Retry-After absorbed 20 min of 429s; no alerts
```

Each scenario runs simulated traffic, one request every 20 s, on a simulated clock. Faults are
injected at set times, and the real agent, resilience code and alert rules run unchanged. A
negative control claims an outage is silent, and the game day has to disagree.

## Run it

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt      # Windows: .venv\Scripts\pip
```

```bash
pytest
```

```bash
python -m opsagents.gameday
```

```bash
python -m opsagents.scheduled eval-drift --state .state
```

```bash
python -m opsagents.slo > deploy/prometheus-rules.yaml
```

## What's real and what's simulated

- The **OpenTelemetry SDK** (1.45.2) is real, with in-memory exporters. In production, use OTLP
  exporters and a collector; the instrumentation doesn't change.
- The **provider** is a fake with scripted faults. `classify()` maps the Anthropic SDK's errors
  (429, 529, 5xx, timeouts, `Retry-After`) onto the same types, but this lab doesn't call the real
  API.
- The **Prometheus rule names** assume the usual OpenTelemetry-to-Prometheus translation (dots to
  underscores, `_total` on counters, `_seconds` on durations). Check them against your exporter
  before relying on them. The rules haven't been loaded into a real Prometheus.

## Layout

| Path | What it is |
|---|---|
| [opsagents/telemetry.py](opsagents/telemetry.py) | Tracer, meter and instruments |
| [opsagents/provider.py](opsagents/provider.py) | Fault-injecting provider; error types and `classify()` |
| [opsagents/resilience.py](opsagents/resilience.py) | Retry, circuit breaker, request and tenant budgets |
| [opsagents/agent.py](opsagents/agent.py) | The defended, instrumented agent |
| [opsagents/slo.py](opsagents/slo.py) | SLOs, burn-rate and spend alerts, Prometheus rules |
| [opsagents/scheduled.py](opsagents/scheduled.py) | Idempotent, locked, deadline-bound scheduled jobs |
| [opsagents/gameday.py](opsagents/gameday.py) | Fault-injection scenarios |
| [runbooks/](runbooks) | What to do when each alert fires |
| [deploy/](deploy) | Generated PrometheusRule, CronJobs |

## Articles

| # | Article |
|---|---|
| 01 | [What Operating an Agent Means](articles/01-what-operating-means.md) |
| 02 | [Tracing Agents with OpenTelemetry](articles/02-tracing-agents.md) |
| 03 | [Metrics That Matter](articles/03-metrics-that-matter.md) |
| 04 | [When the Provider Has a Bad Day](articles/04-when-the-provider-has-a-bad-day.md) |
| 05 | [Budgets That Stop Runaway Spend](articles/05-budgets.md) |
| 06 | [SLOs and Burn-Rate Alerts](articles/06-slos-and-burn-rate-alerts.md) |
| 07 | [Scheduled Agents](articles/07-scheduled-agents.md) |
| 08 | [Game Days, Runbooks, and the Capstone](articles/08-game-days-and-the-capstone.md) |

## How to test this

The [AI Production Monitoring Lab](https://github.com/K11-Software-Solutions/ai-production-monitoring-lab)
covers drift and quality monitoring on live traffic. The
[AI Performance and Cost Lab](https://github.com/K11-Software-Solutions/ai-performance-cost-lab)
covers load testing and cost per good answer.

## Licence and citation

Code: [MIT](LICENSE). Articles: [CC BY 4.0](articles/LICENSE.md). Cite with [CITATION.cff](CITATION.cff).
Kestrel Labs is fictional.
