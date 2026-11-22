"""Staying up when the model provider isn't, and staying inside budget when the agent isn't.

    retry           exponential backoff with full jitter; honours Retry-After; only retryable errors
    CircuitBreaker  after N consecutive failures, stop calling for a cool-down; then let one probe through
    RequestBudget   hard limits per request: model calls, tokens, dollars
    TenantBudgets   daily spend per tenant; refuses work once a tenant's budget is used up

The SDKs retry a little on their own (the Anthropic SDK retries 429 and 5xx twice). These
policies sit above that: they decide when to stop trying, and what to do instead.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from opsagents.provider import ProviderError


class BudgetExceeded(Exception):
    pass


class CircuitOpen(Exception):
    pass


def retry(fn: Callable, attempts: int = 3, base_s: float = 0.2, cap_s: float = 5.0, sleep=time.sleep,
          rng: random.Random | None = None, on_retry: Callable | None = None):
    rng = rng or random.Random()
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except ProviderError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            delay = exc.retry_after if exc.retry_after is not None else rng.uniform(0, min(cap_s, base_s * 2 ** (attempt - 1)))
            if on_retry:
                on_retry(attempt, exc, delay)
            sleep(delay)


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    cooldown_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    failures: int = 0
    opened_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown_s else "open"

    def call(self, fn: Callable):
        with self._lock:
            if self.state == "open":
                raise CircuitOpen(f"provider circuit open; retry after {self.cooldown_s:.0f}s cool-down")
        try:
            result = fn()
        except ProviderError:
            with self._lock:
                self.failures += 1
                if self.state == "half_open" or self.failures >= self.failure_threshold:
                    self.opened_at = self.clock()             # (re)open: the probe failed, or too many failures
            raise
        with self._lock:
            self.failures, self.opened_at = 0, None
        return result


@dataclass
class RequestBudget:
    max_model_calls: int = 6
    max_tokens: int = 40_000
    max_cost_usd: float = 0.25
    model_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    def check_before_call(self) -> None:
        if self.model_calls >= self.max_model_calls:
            raise BudgetExceeded(f"model-call limit {self.max_model_calls} reached")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"token limit {self.max_tokens} reached")
        if self.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"cost limit ${self.max_cost_usd:.2f} reached")

    def record(self, tokens: int, cost: float) -> None:
        self.model_calls += 1
        self.tokens += tokens
        self.cost_usd += cost


@dataclass
class TenantBudgets:
    daily_usd: dict[str, float]
    default_daily_usd: float = 5.0
    spent: dict[tuple[str, str], float] = field(default_factory=dict)
    today: Callable[[], str] = lambda: time.strftime("%Y-%m-%d", time.gmtime())

    def remaining(self, tenant: str) -> float:
        return self.daily_usd.get(tenant, self.default_daily_usd) - self.spent.get((tenant, self.today()), 0.0)

    def check(self, tenant: str) -> None:
        if self.remaining(tenant) <= 0:
            raise BudgetExceeded(f"tenant {tenant} has used today's budget")

    def charge(self, tenant: str, cost: float) -> None:
        key = (tenant, self.today())
        self.spent[key] = self.spent.get(key, 0.0) + cost
