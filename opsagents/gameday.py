"""Game day: break things on purpose and check the system responds the way the runbooks say.

Each scenario replays simulated traffic (one request every 20 s, on a simulated clock) against
the real agent, resilience code and alert rules, with provider faults injected at set times. It
then compares the alerts that fired, and the outcomes callers saw, with what was expected.

    python -m opsagents.gameday             # all scenarios; exit 1 on any mismatch

    provider-outage      primary down 30 min, then both models down 10 min
    runaway-output       after 100 min the model starts writing 50,000-token answers
    noisy-tenant         one tenant sends 10x traffic against a small budget
    rate-limit-storm     every first attempt gets 429 with Retry-After for 20 min
"""
from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass, field

from opsagents import slo
from opsagents.agent import Agent
from opsagents.provider import FakeProvider
from opsagents.resilience import CircuitBreaker, TenantBudgets
from opsagents.telemetry import setup

STEP_S = 20
REQUESTS = ["Remind reviewers when a pull request waits too long", "Export the sprint report as a PDF",
            "Add a Slack integration for deploys", "Generate release notes from merged pull requests"]


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


@dataclass
class Report:
    name: str
    expected_alerts: set
    fired: set
    outcomes: dict
    checks: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.fired == self.expected_alerts and all(self.checks.values())


def simulate(name, minutes, faults_at, expected_alerts, tenants=None, tenant_budgets=None, checks=None) -> Report:
    """faults_at(minute) -> (primary fault, fallback fault, output tokens). tenants(i) -> tenant for request i."""
    clock = Clock()
    log = io.StringIO()
    primary, fallback = FakeProvider(), FakeProvider(model="claude-haiku-5-5")
    budgets = TenantBudgets(daily_usd=tenant_budgets or {}, default_daily_usd=100.0, today=lambda: "sim-day")
    agent = Agent(primary, fallback, setup(), tenants=budgets, sleep=clock.sleep, log=log, clock=clock,
                  breaker=CircuitBreaker(failure_threshold=3, cooldown_s=60, clock=clock),
                  fallback_breaker=CircuitBreaker(failure_threshold=3, cooldown_s=60, clock=clock))
    results, start, fired = [], clock.now, set()
    for i in range(int(minutes * 60 / STEP_S)):
        minute = (clock.now - start) / 60
        p_fault, f_fault, out_tokens = faults_at(minute)
        primary.default_fault, fallback.default_fault = p_fault, f_fault
        primary.output_tokens = fallback.output_tokens = out_tokens
        tenant = tenants(i) if tenants else "default"
        results.append((minute, tenant, agent.run(REQUESTS[i % len(REQUESTS)], tenant=tenant)))
        clock.sleep(STEP_S)
        if i % 15 == 0:                                   # evaluate alerts every 5 simulated minutes
            events = [json.loads(line) for line in log.getvalue().splitlines()]
            for a in slo.slo_alerts(events, clock.now) + slo.spend_alerts(events, clock.now, tenant_budgets or {}):
                fired.add(f"{a['alert']}:{a['severity']}" + (f":{a['tenant']}" if "tenant" in a else ""))
    outcomes: dict[str, int] = {}
    for _, _, r in results:
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
    report = Report(name, expected_alerts, fired, outcomes)
    report.checks = {k: f(results, primary, fallback) for k, f in (checks or {}).items()}
    return report


def provider_outage() -> Report:
    def faults(m):
        if 60 <= m < 90:
            return "529", None, 120            # primary down: fallback serves
        if 90 <= m < 100:
            return "529", "500", 120           # both down: degraded
        return None, None, 120
    # The page fires while it's happening; the slower ticket rule keeps firing for a while after.
    return simulate("provider-outage", 130, faults, {"full_quality_burn:page", "full_quality_burn:ticket"}, checks={
        "fallback answered during the primary outage": lambda r, p, f: any(x.outcome == "fallback" for m, _, x in r if 60 <= m < 90),
        "degraded answers were labelled, not errors": lambda r, p, f: all(x.outcome == "degraded" for m, _, x in r if 91 <= m < 100),
        "no request failed outright": lambda r, p, f: not any(x.outcome == "error" for _, _, x in r),
        # 90 requests during the outage, 3 attempts each without a breaker: the breaker must cut that sharply
        "the circuit breaker spared the primary": lambda r, p, f: p.calls < 2 * len(r) + 120,
    })


def runaway_output() -> Report:
    return simulate("runaway-output", 150, lambda m: (None, None, 50_000 if m >= 100 else 120),
                    {"cost_burn:page", "cost_burn:ticket", "spend_spike:page"}, checks={
        "per-request budget stopped further calls": lambda r, p, f: all(x.model_calls <= 6 for _, _, x in r),
        # each runaway answer costs about $1; the tenant's $100 daily budget is the last line of defence
        "the daily budget stopped the runaway spend": lambda r, p, f: any(x.outcome == "budget_exceeded" for m, _, x in r if m >= 100),
        "nothing was refused before the runaway began": lambda r, p, f: all(x.outcome == "ok" for m, _, x in r if m < 100),
    })


def noisy_tenant() -> Report:
    tenants = lambda i: "quiet" if i % 10 == 0 else "noisy"            # noqa: E731
    return simulate("noisy-tenant", 90, lambda m: (None, None, 120), {"tenant_budget:ticket:noisy"},
                    tenants=tenants, tenant_budgets={"noisy": 0.5, "quiet": 5.0}, checks={
        "the noisy tenant was cut off": lambda r, p, f: any(x.outcome == "budget_exceeded" for _, t, x in r if t == "noisy"),
        "the quiet tenant was unaffected": lambda r, p, f: all(x.outcome == "ok" for _, t, x in r if t == "quiet"),
    })


def rate_limit_storm() -> Report:
    def faults(m):
        return None, None, 120

    report = simulate("rate-limit-storm", 90, faults, set())
    # Re-run with 429 on every first attempt between minutes 30 and 50: retries absorb it
    clock = Clock()
    log = io.StringIO()
    primary = FakeProvider()
    agent = Agent(primary, FakeProvider(model="claude-haiku-5-5"), setup(), sleep=clock.sleep, log=log, clock=clock,
                  breaker=CircuitBreaker(failure_threshold=3, cooldown_s=60, clock=clock))
    outcomes = {}
    for i in range(int(90 * 60 / STEP_S)):
        minute = i * STEP_S / 60
        primary.faults = ["429"] if 30 <= minute < 50 else []
        r = agent.run(REQUESTS[i % len(REQUESTS)])
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        clock.sleep(STEP_S)
    events = [json.loads(line) for line in log.getvalue().splitlines()]
    fired = {f"{a['alert']}:{a['severity']}" for a in slo.slo_alerts(events, clock.now)}
    report.fired, report.outcomes = fired, outcomes
    report.checks = {"every request was answered by the primary model": outcomes == {"ok": len(events)}}
    return report


SCENARIOS = {"provider-outage": provider_outage, "runaway-output": runaway_output, "noisy-tenant": noisy_tenant,
             "rate-limit-storm": rate_limit_storm}


def main(argv: list[str] | None = None) -> int:
    names = (argv if argv is not None else sys.argv[1:]) or list(SCENARIOS)
    ok = True
    for name in names:
        r = SCENARIOS[name]()
        ok &= r.passed
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.name}")
        print(f"      alerts   expected {sorted(r.expected_alerts) or '[]'}  fired {sorted(r.fired) or '[]'}")
        print(f"      outcomes {r.outcomes}")
        for check, passed in r.checks.items():
            print(f"      {'ok ' if passed else 'NO '} {check}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
