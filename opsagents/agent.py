"""The triage agent, instrumented and defended.

One request:
    1. tenant budget check (refuse early if today's budget is used up)
    2. the agent loop, each model call wrapped in: request budget -> circuit breaker -> retry
    3. if the primary model is unavailable (breaker open, retries exhausted): the fallback model
    4. if that fails too: degraded mode, the rules baseline, clearly marked as degraded
    5. one trace, metrics, and one JSON log event with the outcome

Outcomes: ok, fallback, degraded, budget_exceeded, error. A degraded answer is still an answer,
and it is labelled so the caller (and the SLOs) know.
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from dataclasses import dataclass, field

from opentelemetry.trace import Status, StatusCode

from opsagents.provider import ProviderError, cost_usd
from opsagents.resilience import BudgetExceeded, CircuitBreaker, CircuitOpen, RequestBudget, TenantBudgets, retry
from opsagents.telemetry import Telemetry

BACKLOG = [
    {"id": "KES-101", "title": "Export sprint report as PDF"},
    {"id": "KES-103", "title": "Notify reviewers when a pull request waits too long"},
    {"id": "KES-104", "title": "Dark mode for the dashboard"},
    {"id": "KES-105", "title": "Generate release notes from merged pull requests"},
]
WORD = re.compile(r"[a-z]+")
STOP = frozenset("a an and as for from in is of on or the to when with".split())


def terms(text: str) -> set[str]:
    return {w.rstrip("s") for w in WORD.findall(text.lower()) if w not in STOP}


def search_backlog(query: str) -> list[dict]:
    q = terms(query)
    hits = [{"id": s["id"], "title": s["title"], "score": len(q & terms(s["title"]))} for s in BACKLOG]
    return sorted([h for h in hits if h["score"]], key=lambda h: -h["score"])[:3]


TOOLS = {"search_backlog": lambda args: search_backlog(args["query"])}


def rules_baseline(request: str) -> dict:
    hits = search_backlog(request)
    if hits and hits[0]["score"] >= 3:
        return {"decision": "duplicate", "story_id": hits[0]["id"]}
    return {"decision": "new", "story_id": None}


@dataclass
class Result:
    outcome: str
    answer: dict | None
    model: str | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    request_id: str
    notes: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, primary, fallback, telemetry: Telemetry, tenants: TenantBudgets | None = None,
                 breaker: CircuitBreaker | None = None, fallback_breaker: CircuitBreaker | None = None,
                 budget_factory=RequestBudget, sleep=time.sleep, log=sys.stdout, variant: str = "stable", clock=time.time):
        self.primary, self.fallback, self.t = primary, fallback, telemetry
        self.tenants = tenants or TenantBudgets(daily_usd={})
        self.breakers = {"primary": breaker or CircuitBreaker(), "fallback": fallback_breaker or CircuitBreaker()}
        self.budget_factory, self.sleep, self.log, self.variant, self.clock = budget_factory, sleep, log, variant, clock

    # --- one model call, defended and traced ---------------------------------------------------

    def _chat(self, which: str, provider, messages, budget: RequestBudget, route: str):
        budget.check_before_call()
        with self.t.tracer.start_as_current_span(f"chat {provider.model}") as span:
            span.set_attributes({"gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic",
                                 "gen_ai.request.model": provider.model, "kestrel.provider.role": which})
            start = time.perf_counter()

            def attempt():
                return self.breakers[which].call(lambda: provider.chat(messages, list(TOOLS)))

            def note_retry(n, exc, delay):
                span.add_event("retry", {"attempt": n, "error.type": type(exc).__name__, "delay_s": round(delay, 3)})

            try:
                reply = retry(attempt, sleep=self.sleep, on_retry=note_retry)
            except (ProviderError, CircuitOpen) as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.set_attribute("error.type", type(exc).__name__)
                raise
            seconds = time.perf_counter() - start
            cost = cost_usd(reply.model, reply.input_tokens, reply.output_tokens)
            span.set_attributes({"gen_ai.response.model": reply.model, "gen_ai.usage.input_tokens": reply.input_tokens,
                                 "gen_ai.usage.output_tokens": reply.output_tokens,
                                 "gen_ai.response.finish_reasons": [reply.finish_reason], "kestrel.cost_usd": cost})
            attrs = {"gen_ai.request.model": provider.model, "kestrel.route": route, "kestrel.variant": self.variant}
            self.t.tokens.add(reply.input_tokens, {**attrs, "gen_ai.token.type": "input"})
            self.t.tokens.add(reply.output_tokens, {**attrs, "gen_ai.token.type": "output"})
            self.t.duration.record(seconds, attrs)
            budget.record(reply.input_tokens + reply.output_tokens, cost)
            return reply, cost

    def _loop(self, which: str, provider, request: str, budget: RequestBudget, route: str) -> tuple[dict, dict]:
        messages = [{"role": "user", "content": request}]
        totals = {"input": 0, "output": 0, "cost": 0.0, "model": provider.model}
        while True:
            reply, cost = self._chat(which, provider, messages, budget, route)
            totals["input"] += reply.input_tokens
            totals["output"] += reply.output_tokens
            totals["cost"] += cost
            if reply.tool is None:
                return json.loads(reply.text), totals
            with self.t.tracer.start_as_current_span(f"execute_tool {reply.tool}") as tool_span:
                tool_span.set_attributes({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": reply.tool})
                output = TOOLS[reply.tool](reply.args)
            messages += [{"role": "assistant", "content": f"call {reply.tool}"}, {"role": "tool", "content": json.dumps(output)}]

    # --- one request ---------------------------------------------------------------------------

    def run(self, request: str, tenant: str = "default", route: str = "triage") -> Result:
        rid, start = uuid.uuid4().hex, time.perf_counter()
        budget = self.budget_factory()
        notes: list[str] = []
        with self.t.tracer.start_as_current_span("invoke_agent triage") as root:
            root.set_attributes({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "kestrel-triage",
                                 "kestrel.tenant": tenant, "kestrel.request_id": rid})
            answer, outcome, totals = None, "error", {"input": 0, "output": 0, "cost": 0.0, "model": None}
            try:
                self.tenants.check(tenant)
                for which, provider, label in (("primary", self.primary, "ok"), ("fallback", self.fallback, "fallback")):
                    try:
                        answer, totals = self._loop(which, provider, request, budget, route)
                        outcome = label
                        break
                    except (ProviderError, CircuitOpen) as exc:
                        notes.append(f"{which} unavailable: {type(exc).__name__}")
                if answer is None:
                    answer, outcome, totals["model"] = rules_baseline(request), "degraded", "rules-baseline"
                    notes.append("answered by the rules baseline")
            except BudgetExceeded as exc:
                outcome = "budget_exceeded"
                notes.append(str(exc))
            totals["cost"] = budget.cost_usd                         # everything spent, including failed attempts
            self.tenants.charge(tenant, totals["cost"])
            root.set_attributes({"kestrel.outcome": outcome, "kestrel.cost_usd": totals["cost"], "kestrel.model_calls": budget.model_calls})
            if outcome in ("error", "budget_exceeded"):
                root.set_status(Status(StatusCode.ERROR, outcome))
        latency = (time.perf_counter() - start) * 1000
        self.t.cost.add(totals["cost"], {"kestrel.route": route, "kestrel.variant": self.variant, "kestrel.tenant": tenant})
        self.t.requests.add(1, {"kestrel.route": route, "kestrel.outcome": outcome})
        self.t.request_duration.record(latency / 1000, {"kestrel.route": route})
        if totals["cost"] > 0.05:
            self.t.expensive.add(1, {"kestrel.route": route})
        result = Result(outcome, answer, totals["model"], budget.model_calls, totals["input"], totals["output"],
                        round(totals["cost"], 6), round(latency, 1), rid, notes)
        print(json.dumps({"event": "request", "ts": round(self.clock(), 3), "request_id": rid, "route": route,
                          "tenant": tenant, "variant": self.variant, "outcome": outcome, "model": totals["model"],
                          "status": 200 if outcome in ("ok", "fallback", "degraded") else 429 if outcome == "budget_exceeded" else 503,
                          "input_tokens": totals["input"], "output_tokens": totals["output"],
                          "cost_usd": result.cost_usd, "latency_ms": result.latency_ms}), file=self.log, flush=True)
        return result
