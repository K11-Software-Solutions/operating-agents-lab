"""A model provider that can fail on purpose.

Operating an agent means planning for the provider having a bad day: rate limits (429), overload
(529), server errors (500), timeouts, and slow responses. FakeProvider produces each of these on
a script, so retries, circuit breaking, fallbacks and alerts can be tested deterministically.

The real provider path (Claude) raises the same exception types, so the resilience code is the
same for both: anthropic.RateLimitError and friends are mapped by `classify()`.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

PRICES_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50),
                   "rules-baseline": (0.0, 0.0)}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES_PER_MTOK.get(model, (0.0, 0.0))
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


class ProviderError(Exception):
    retryable = False

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(ProviderError):
    retryable = True


class Overloaded(ProviderError):
    retryable = True


class ServerError(ProviderError):
    retryable = True


class Timeout(ProviderError):
    retryable = True


class BadRequest(ProviderError):
    retryable = False


@dataclass
class Reply:
    model: str
    tool: str | None            # a tool to call, or None for a final answer
    args: dict | None
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str


@dataclass
class FakeProvider:
    """faults: a list consumed one per call, e.g. ["429", "ok", "529"]; "ok" or None means succeed.
    after the list runs out, `default_fault` applies (None = succeed)."""
    model: str = "claude-opus-5-5"
    faults: list = field(default_factory=list)
    default_fault: str | None = None
    latency_s: float = 0.0
    output_tokens: int = 120
    calls: int = 0

    def chat(self, messages: list[dict], tools: list[str]) -> Reply:
        self.calls += 1
        fault = self.faults.pop(0) if self.faults else self.default_fault
        if self.latency_s:
            time.sleep(self.latency_s)
        if fault == "429":
            raise RateLimited("rate limited", retry_after=0.01)
        if fault == "529":
            raise Overloaded("overloaded")
        if fault == "500":
            raise ServerError("internal error")
        if fault == "timeout":
            raise Timeout("request timed out")
        if fault == "400":
            raise BadRequest("invalid request")
        input_tokens = sum(len(json.dumps(m)) for m in messages) // 4 + 300
        observed = [m for m in messages if m["role"] == "tool"]
        if not observed:
            request = messages[0]["content"]
            return Reply(self.model, "search_backlog", {"query": request}, "", input_tokens, 40, "tool_use")
        hits = json.loads(observed[-1]["content"])
        decision = {"decision": "duplicate" if hits and hits[0]["score"] >= 3 else "new",
                    "story_id": hits[0]["id"] if hits and hits[0]["score"] >= 3 else None}
        return Reply(self.model, None, None, json.dumps(decision), input_tokens, self.output_tokens, "end_turn")


def classify(exc: Exception) -> ProviderError:
    """Map a real SDK exception onto these types (so the same retry and breaker logic applies)."""
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status == 429 or name == "RateLimitError":
        headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
        ra = headers.get("retry-after")
        return RateLimited(str(exc), retry_after=float(ra) if ra else None)
    if status == 529 or name == "OverloadedError":
        return Overloaded(str(exc))
    if name in ("APITimeoutError", "TimeoutError"):
        return Timeout(str(exc))
    if status and status >= 500:
        return ServerError(str(exc))
    return BadRequest(str(exc))
