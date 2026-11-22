"""Lab 9 tests: resilience, telemetry, budgets, SLO alerts, scheduled jobs, and the game day."""
import io
import json
import os
import random
import time
from pathlib import Path

import pytest
import yaml

from opsagents import gameday, slo
from opsagents.agent import Agent
from opsagents.provider import BadRequest, FakeProvider, Overloaded, RateLimited, classify
from opsagents.resilience import BudgetExceeded, CircuitBreaker, CircuitOpen, RequestBudget, TenantBudgets, retry
from opsagents.scheduled import JobContext, backlog_hygiene, eval_drift, run_job
from opsagents.telemetry import setup

ROOT = Path(__file__).resolve().parents[1]
DUP = "Remind reviewers when a pull request waits too long"


def agent(primary=None, fallback=None, **kw):
    return Agent(primary or FakeProvider(), fallback or FakeProvider(model="claude-haiku-5-5"), kw.pop("t", setup()),
                 sleep=lambda s: None, log=io.StringIO(), **kw)


# --- retry -------------------------------------------------------------------------------------

def test_retry_backs_off_with_jitter_and_then_succeeds():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Overloaded("busy")
        return "ok"
    assert retry(flaky, attempts=3, base_s=1, sleep=sleeps.append, rng=random.Random(1)) == "ok"
    assert len(sleeps) == 2 and 0 <= sleeps[0] <= 1 and 0 <= sleeps[1] <= 2


def test_retry_honours_retry_after():
    sleeps, n = [], iter([RateLimited("slow down", retry_after=7.5), None])

    def fn():
        e = next(n)
        if e:
            raise e
        return "ok"
    retry(fn, sleep=sleeps.append)
    assert sleeps == [7.5]


def test_non_retryable_errors_are_not_retried():
    calls = []

    def bad():
        calls.append(1)
        raise BadRequest("invalid")
    with pytest.raises(BadRequest):
        retry(bad, sleep=lambda s: None)
    assert len(calls) == 1


def test_classify_maps_sdk_style_errors():
    class E(Exception):
        def __init__(self, status):
            self.status_code = status
    assert isinstance(classify(E(429)), RateLimited) and isinstance(classify(E(529)), Overloaded)
    assert classify(E(503)).retryable and not classify(E(400)).retryable


# --- circuit breaker ---------------------------------------------------------------------------

def test_breaker_opens_cools_down_and_probes():
    now = [0.0]
    b = CircuitBreaker(failure_threshold=2, cooldown_s=30, clock=lambda: now[0])

    def fail():
        raise Overloaded("busy")
    for _ in range(2):
        with pytest.raises(Overloaded):
            b.call(fail)
    assert b.state == "open"
    with pytest.raises(CircuitOpen):
        b.call(lambda: "never called")
    now[0] = 31
    assert b.state == "half_open"
    with pytest.raises(Overloaded):
        b.call(fail)                                # the probe fails: open again
    assert b.state == "open"
    now[0] = 62
    assert b.call(lambda: "ok") == "ok" and b.state == "closed"


# --- budgets -----------------------------------------------------------------------------------

def test_request_budget_limits():
    b = RequestBudget(max_model_calls=2)
    b.record(10, 0.01)
    b.record(10, 0.01)
    with pytest.raises(BudgetExceeded, match="model-call limit"):
        b.check_before_call()
    b = RequestBudget(max_cost_usd=0.01)
    b.record(10, 0.02)
    with pytest.raises(BudgetExceeded, match="cost limit"):
        b.check_before_call()


def test_tenant_budgets_are_per_tenant_and_per_day():
    day = ["d1"]
    tb = TenantBudgets(daily_usd={"a": 1.0}, today=lambda: day[0])
    tb.charge("a", 1.0)
    with pytest.raises(BudgetExceeded):
        tb.check("a")
    tb.check("b")                                   # another tenant is unaffected
    day[0] = "d2"
    tb.check("a")                                   # a new day resets


# --- the agent ---------------------------------------------------------------------------------

def test_outcomes_under_each_failure_mode():
    assert agent().run(DUP).outcome == "ok"
    assert agent(primary=FakeProvider(default_fault="529")).run(DUP).outcome == "fallback"
    r = agent(primary=FakeProvider(default_fault="529"), fallback=FakeProvider(default_fault="500")).run(DUP)
    assert r.outcome == "degraded" and r.answer == {"decision": "duplicate", "story_id": "KES-103"} and r.model == "rules-baseline"
    a = agent(tenants=TenantBudgets(daily_usd={"t": 0.0}))
    assert a.run(DUP, tenant="t").outcome == "budget_exceeded"


def test_a_non_retryable_error_falls_back_without_retrying():
    p = FakeProvider(default_fault="400")
    assert agent(primary=p).run(DUP).outcome == "fallback" and p.calls == 1


def test_trace_shape_and_genai_attributes():
    t = setup()
    agent(t=t).run(DUP, tenant="acme")
    spans = {s.name: s for s in t.finished_spans()}
    root = spans["invoke_agent triage"]
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent" and root.attributes["kestrel.outcome"] == "ok"
    chat = [s for s in t.finished_spans() if s.name.startswith("chat ")]
    assert len(chat) == 2 and all(s.parent.span_id == root.context.span_id for s in chat)
    assert chat[0].attributes["gen_ai.request.model"] == "claude-opus-5-5"
    assert chat[0].attributes["gen_ai.usage.input_tokens"] > 0 and chat[-1].attributes["gen_ai.response.finish_reasons"] == ("end_turn",)
    assert spans["execute_tool search_backlog"].attributes["gen_ai.tool.name"] == "search_backlog"


def test_retries_are_visible_in_the_trace():
    t = setup()
    agent(primary=FakeProvider(faults=["429", "ok"]), t=t).run(DUP)
    chat = next(s for s in t.finished_spans() if s.name.startswith("chat "))
    assert [e.name for e in chat.events] == ["retry"] and chat.events[0].attributes["error.type"] == "RateLimited"


def test_metrics_carry_tokens_cost_and_outcomes():
    t = setup()
    a = agent(t=t)
    a.run(DUP, tenant="acme")
    m = t.metric_points()
    types = {attrs["gen_ai.token.type"] for attrs, _ in m["gen_ai.client.token.usage"]}
    assert types == {"input", "output"}
    assert m["kestrel.agent.cost_usd"][0][0]["kestrel.tenant"] == "acme" and m["kestrel.agent.cost_usd"][0][1] > 0
    assert m["kestrel.agent.requests"][0][0]["kestrel.outcome"] == "ok"
    assert m["kestrel.agent.request.duration"][0][1]["count"] == 1


def test_one_log_event_per_request_without_the_request_text():
    log = io.StringIO()
    Agent(FakeProvider(), FakeProvider(), setup(), sleep=lambda s: None, log=log).run("secret project nightingale")
    events = [json.loads(x) for x in log.getvalue().splitlines()]
    assert len(events) == 1 and events[0]["outcome"] == "ok" and "nightingale" not in log.getvalue()


# --- SLOs and alerts ---------------------------------------------------------------------------

def events(n, outcome="ok", start=0.0, step=20.0, cost=0.006, latency=500.0, tenant="default"):
    return [{"ts": start + i * step, "outcome": outcome, "cost_usd": cost, "latency_ms": latency, "tenant": tenant} for i in range(n)]


def test_healthy_traffic_raises_nothing():
    e = events(1000)
    assert slo.slo_alerts(e, now=e[-1]["ts"]) == []


def test_a_sustained_outage_pages():
    e = events(180) + events(30, outcome="degraded", start=180 * 20)
    fired = {(a["alert"], a["severity"]) for a in slo.slo_alerts(e, now=e[-1]["ts"])}
    assert ("full_quality_burn", "page") in fired
    assert not any(a == "availability_burn" for a, _ in fired)      # degraded answers are still answers


def test_an_outage_that_has_ended_does_not_page():
    e = events(180) + events(30, outcome="degraded", start=3600) + events(30, start=3600 + 600)
    fired = {(a["alert"], a["severity"]) for a in slo.slo_alerts(e, now=e[-1]["ts"])}
    assert ("full_quality_burn", "page") not in fired                # the 5 min window has recovered


def test_budget_refusals_do_not_burn_availability():
    e = events(100) + events(100, outcome="budget_exceeded", start=2000)
    assert not [a for a in slo.slo_alerts(e, now=e[-1]["ts"]) if a["slo"] == "availability"]


def test_burn_rate_math():
    e = events(99) + events(1, outcome="error", start=99 * 20)
    rate = slo.burn_rate(e, slo.SLOS[0], now=e[-1]["ts"], window_s=10_000)
    assert rate == pytest.approx(0.01 / 0.005)


def test_spend_alerts():
    e = events(360, cost=0.01) + events(90, cost=0.5, start=360 * 20)
    names = {a["alert"] for a in slo.spend_alerts(e, now=e[-1]["ts"], tenant_budgets={"default": 20.0})}
    assert names == {"spend_spike", "tenant_budget"}
    short = events(20, cost=0.01) + events(20, cost=0.5, start=400)
    assert slo.spend_alerts(short, now=short[-1]["ts"], tenant_budgets={}) == []     # too little history to judge


def test_prometheus_rules_file_is_generated_from_the_slos():
    committed = yaml.safe_load((ROOT / "deploy" / "prometheus-rules.yaml").read_text(encoding="utf-8"))
    assert committed == slo.prometheus_rules(), "run: python -m opsagents.slo > deploy/prometheus-rules.yaml"
    rules = committed["spec"]["groups"][0]["rules"]
    assert len(rules) == len(slo.SLOS) * len(slo.WINDOWS) + 1
    assert all(r["labels"]["severity"] in ("page", "ticket") and r["annotations"]["runbook_url"] for r in rules)
    for r in rules:
        assert (ROOT / r["annotations"]["runbook_url"]).exists(), r["annotations"]["runbook_url"]


# --- scheduled jobs ----------------------------------------------------------------------------

def test_jobs_are_idempotent_per_run_key(tmp_path):
    first = run_job("hygiene", backlog_hygiene, tmp_path, run_key="2026-11-22")
    again = run_job("hygiene", backlog_hygiene, tmp_path, run_key="2026-11-22")
    assert first.status == "succeeded" and again.status == "skipped" and again.output == first.output
    assert run_job("hygiene", backlog_hygiene, tmp_path, run_key="2026-11-23").status == "succeeded"


def test_overlapping_runs_are_refused_but_stale_locks_are_broken(tmp_path):
    (tmp_path / "hygiene.lock").write_text("12345")
    assert run_job("hygiene", backlog_hygiene, tmp_path, run_key="k", max_runtime_s=900).status == "locked"
    old = time.time() - 2000
    os.utime(tmp_path / "hygiene.lock", (old, old))
    assert run_job("hygiene", backlog_hygiene, tmp_path, run_key="k", max_runtime_s=900).status == "succeeded"
    assert not (tmp_path / "hygiene.lock").exists()


def test_a_failed_run_leaves_nothing_so_it_can_retry(tmp_path):
    def boom(ctx):
        raise RuntimeError("provider down")
    r = run_job("x", boom, tmp_path, run_key="k")
    assert r.status == "failed" and not list(tmp_path.glob("x.k.json")) and not (tmp_path / "x.lock").exists()


def test_jobs_stop_at_the_deadline_and_the_budget():
    now = [0.0]
    ctx = JobContext(deadline=-1, budget_usd=1.0, clock=lambda: now[0])
    assert backlog_hygiene(ctx)["complete"] is False
    ctx = JobContext(deadline=1e9, budget_usd=0.0)
    out = eval_drift(ctx)
    assert out["attempted"] == 0 and out["drifted"] is True                       # a run that did nothing is not a pass


def test_eval_drift_notices_a_worse_model():
    bad = Agent(FakeProvider(default_fault="529"), FakeProvider(default_fault="529"), setup(), sleep=lambda s: None, log=io.StringIO())
    out = eval_drift(JobContext(deadline=1e9, budget_usd=1.0), agent=bad)
    assert out["drifted"] is True and out["accuracy"] == 0.0          # degraded answers don't count as correct


def test_cronjobs_are_safe_to_retry():
    for job in yaml.safe_load_all((ROOT / "deploy" / "cronjobs.yaml").read_text(encoding="utf-8")):
        spec = job["spec"]
        assert spec["concurrencyPolicy"] == "Forbid" and spec["startingDeadlineSeconds"] > 0
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert spec["jobTemplate"]["spec"]["activeDeadlineSeconds"] > 0


# --- game day ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(gameday.SCENARIOS))
def test_game_day(name):
    report = gameday.SCENARIOS[name]()
    assert report.passed, (report.fired, report.expected_alerts, report.checks)


def test_game_day_can_fail():
    """Negative control: claim an outage raises no alerts, and the game day must disagree."""
    def faults(m):
        return ("529", "500", 120) if 60 <= m < 90 else (None, None, 120)
    report = gameday.simulate("outage-claimed-silent", 100, faults, expected_alerts=set())
    assert not report.passed and "full_quality_burn:page" in report.fired
