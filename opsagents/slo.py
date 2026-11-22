"""Service level objectives, burn-rate alerts and spend alerts, from the request log.

SLOs, each a share of requests that must be "good" over 30 days:

    availability   the caller got an answer, or a deliberate budget refusal      99.5%
    full_quality   the answer came from a model, not the degraded baseline        99.0%
    latency        answered within 10 s                                          95.0%
    cost           cost per request at most $0.05                                99.0%

Alerts use multi-window, multi-burn-rate rules (from Google's SRE workbook): page when the
error budget is burning 14.4x too fast over both 1 h and 5 min; open a ticket at 6x over both
6 h and 30 min. The long window shows it's real; the short one shows it's still happening.

Spend alerts: a tenant past 80% of its daily budget; total spend in the last hour more than 3x
the hourly average of the previous 24 h.

The same definitions render Prometheus rules (`prometheus_rules()`), so the alerts you test here
are the alerts you deploy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import yaml


@dataclass(frozen=True)
class SLO:
    name: str
    target: float
    good: Callable[[dict], bool]
    promql_bad: str            # a selector for bad requests, in the exported metric names
    description: str


SLOS = [
    # A tenant over its own budget gets a deliberate 429: a policy working, not the service failing.
    SLO("availability", 0.995, lambda e: e["outcome"] != "error",
        'kestrel_agent_requests_total{kestrel_outcome="error"}', "the caller got an answer"),
    SLO("full_quality", 0.99, lambda e: e["outcome"] in ("ok", "fallback", "budget_exceeded"),
        'kestrel_agent_requests_total{kestrel_outcome=~"degraded|error"}', "answered by a model, not the degraded baseline"),
    SLO("latency", 0.95, lambda e: e["latency_ms"] <= 10_000,
        'kestrel_agent_request_duration_seconds_count - ignoring(le) kestrel_agent_request_duration_seconds_bucket{le="10.0"}', "answered within 10 s"),
    SLO("cost", 0.99, lambda e: e["cost_usd"] <= 0.05,
        'kestrel_agent_expensive_requests_total', "at most $0.05 per request"),
]
WINDOWS = [  # (severity, burn rate, long window s, short window s)
    ("page", 14.4, 3600, 300),
    ("ticket", 6.0, 6 * 3600, 1800),
]


def burn_rate(events: list[dict], slo: SLO, now: float, window_s: float) -> float | None:
    recent = [e for e in events if now - window_s < e["ts"] <= now]
    if not recent:
        return None
    bad = sum(not slo.good(e) for e in recent)
    return (bad / len(recent)) / (1 - slo.target)


def slo_alerts(events: list[dict], now: float, min_events: int = 10) -> list[dict]:
    out = []
    for slo in SLOS:
        for severity, threshold, long_w, short_w in WINDOWS:
            long_n = sum(1 for e in events if now - long_w < e["ts"] <= now)
            if long_n < min_events:
                continue
            b_long, b_short = burn_rate(events, slo, now, long_w), burn_rate(events, slo, now, short_w)
            if b_long is not None and b_short is not None and b_long >= threshold and b_short >= threshold:
                out.append({"alert": f"{slo.name}_burn", "severity": severity, "slo": slo.name,
                            "burn_long": round(b_long, 1), "burn_short": round(b_short, 1),
                            "summary": f"{slo.name} error budget burning {b_long:.1f}x over {long_w // 60:.0f} min"})
                break                          # page supersedes ticket for the same SLO
    return out


def spend_alerts(events: list[dict], now: float, tenant_budgets: dict[str, float], spike_factor: float = 3.0,
                 day_start: float | None = None) -> list[dict]:
    out = []
    day_start = day_start if day_start is not None else now - 86400
    for tenant, budget in tenant_budgets.items():
        spent = sum(e["cost_usd"] for e in events if e.get("tenant") == tenant and e["ts"] > day_start)
        if budget and spent >= 0.8 * budget:
            out.append({"alert": "tenant_budget", "severity": "ticket", "tenant": tenant,
                        "summary": f"{tenant} has spent ${spent:.2f} of ${budget:.2f} today ({spent / budget:.0%})"})
    last_hour = sum(e["cost_usd"] for e in events if now - 3600 < e["ts"] <= now)
    previous = [e for e in events if now - 25 * 3600 < e["ts"] <= now - 3600]
    history_s = (now - 3600 - min(e["ts"] for e in previous)) if previous else 0
    if history_s >= 3600:                  # with less than an hour of history, any hour looks like a spike
        hourly = sum(e["cost_usd"] for e in previous) / (history_s / 3600)
        if hourly > 0 and last_hour > spike_factor * hourly:
            out.append({"alert": "spend_spike", "severity": "page",
                        "summary": f"${last_hour:.2f} in the last hour, {last_hour / hourly:.1f}x the recent hourly average"})
    return out


def prometheus_rules() -> dict:
    """A PrometheusRule (prometheus-operator) with the same SLOs and burn-rate windows."""
    rules = []
    for slo in SLOS:
        budget = round(1 - slo.target, 6)
        for severity, threshold, long_w, short_w in WINDOWS:
            ratio = lambda w: (f"(sum(rate({slo.promql_bad}[{w}])) / sum(rate(kestrel_agent_requests_total[{w}])))")   # noqa: E731
            lw, sw = f"{long_w // 60}m", f"{short_w // 60}m"
            rules.append({
                "alert": f"Kestrel{slo.name.title().replace('_', '')}Burn{severity.title()}",
                "expr": f"{ratio(lw)} > ({threshold} * {budget}) and {ratio(sw)} > ({threshold} * {budget})",
                "labels": {"severity": severity, "slo": slo.name},
                "annotations": {"summary": f"{slo.name} SLO ({slo.description}) burning {threshold}x over {lw} and {sw}",
                                "runbook_url": f"runbooks/{'provider-outage' if slo.name in ('availability', 'full_quality') else 'cost-or-latency'}.md"},
            })
    rules.append({"alert": "KestrelSpendSpike",
                  "expr": "sum(increase(kestrel_agent_cost_usd_total[1h])) > 3 * (sum(increase(kestrel_agent_cost_usd_total[24h] offset 1h)) / 24)",
                  "labels": {"severity": "page"},
                  "annotations": {"summary": "spend in the last hour is over 3x the recent hourly average", "runbook_url": "runbooks/cost-spike.md"}})
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "kestrel-agents-slos", "labels": {"app.kubernetes.io/name": "kestrel-agents"}},
            "spec": {"groups": [{"name": "kestrel-agents-slos", "rules": rules}]}}


def main() -> int:
    print(yaml.safe_dump(prometheus_rules(), sort_keys=False, width=200), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
