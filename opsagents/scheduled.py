"""Scheduled agents: jobs that run on a timer, with nobody watching.

A scheduled agent fails differently from a request-serving one. It can run twice (a retried
CronJob), run concurrently (a slow run overlapping the next), run forever, or spend all night.
`run_job` guards against each:

    idempotent    a run key (by default the UTC date); a key that already succeeded is skipped
    no overlap    an exclusive lock file; a lock older than the deadline is treated as stale
    deadline      the job checks `ctx.time_left()` and stops cleanly when it runs out
    budget        the job gets a RequestBudget-style cost limit for the whole run
    atomic        results are written to a temp file and renamed, so a crash never leaves half a result

    python -m opsagents.scheduled backlog-hygiene --state /var/lib/kestrel
    python -m opsagents.scheduled eval-drift --state /var/lib/kestrel
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from opsagents.agent import BACKLOG, Agent, terms
from opsagents.provider import FakeProvider
from opsagents.telemetry import setup


@dataclass
class JobContext:
    deadline: float
    budget_usd: float
    spent_usd: float = 0.0
    clock: Callable[[], float] = time.monotonic
    notes: list[str] = field(default_factory=list)

    def time_left(self) -> float:
        return self.deadline - self.clock()

    def can_spend(self, amount: float) -> bool:
        return self.spent_usd + amount <= self.budget_usd


@dataclass
class JobResult:
    status: str             # succeeded | skipped | locked | failed
    run_key: str
    output: dict | None = None
    error: str = ""


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run_job(name: str, fn: Callable[[JobContext], dict], state: Path, run_key: str | None = None,
            max_runtime_s: float = 900, budget_usd: float = 1.0, clock: Callable[[], float] = time.monotonic,
            wall: Callable[[], float] = time.time) -> JobResult:
    state.mkdir(parents=True, exist_ok=True)
    run_key = run_key or time.strftime("%Y-%m-%d", time.gmtime(wall()))
    done = state / f"{name}.{run_key}.json"
    if done.exists():
        return JobResult("skipped", run_key, json.loads(done.read_text(encoding="utf-8")))
    lock = state / f"{name}.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = wall() - lock.stat().st_mtime
        if age < max_runtime_s:
            return JobResult("locked", run_key, error=f"another run holds the lock ({age:.0f}s old)")
        lock.unlink(missing_ok=True)                      # stale: the previous run died past its deadline
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        ctx = JobContext(deadline=clock() + max_runtime_s, budget_usd=budget_usd, clock=clock)
        try:
            output = fn(ctx)
        except Exception as exc:                          # a failed run leaves no result, so it can be retried
            return JobResult("failed", run_key, error=f"{type(exc).__name__}: {exc}")
        output = {**output, "run_key": run_key, "spent_usd": round(ctx.spent_usd, 6), "notes": ctx.notes}
        _write_atomic(done, output)
        return JobResult("succeeded", run_key, output)
    finally:
        lock.unlink(missing_ok=True)


# --- two jobs ----------------------------------------------------------------------------------

def backlog_hygiene(ctx: JobContext) -> dict:
    """Find pairs of backlog stories that look like duplicates. Rules only: cheap enough to run nightly."""
    pairs = []
    for i, a in enumerate(BACKLOG):
        for b in BACKLOG[i + 1:]:
            if ctx.time_left() <= 0:
                ctx.notes.append("stopped at the deadline")
                return {"possible_duplicates": pairs, "complete": False}
            shared = terms(a["title"]) & terms(b["title"])
            if len(shared) >= 2:
                pairs.append({"a": a["id"], "b": b["id"], "shared": sorted(shared)})
    return {"possible_duplicates": pairs, "complete": True}


EVAL_CASES = [("Remind reviewers when a pull request waits too long", "duplicate"),
              ("Export the sprint report as a PDF", "duplicate"),
              ("Add a Slack integration for deploys", "new")]


def eval_drift(ctx: JobContext, agent: Agent | None = None, baseline_accuracy: float = 1.0, tolerance: float = 0.1) -> dict:
    """Re-run a small eval through the live agent each night; flag a drop before users notice."""
    agent = agent or Agent(FakeProvider(), FakeProvider(model="claude-haiku-5-5"), setup(), log=open(os.devnull, "w"))
    correct = attempted = 0
    for request, expected in EVAL_CASES:
        if ctx.time_left() <= 0 or not ctx.can_spend(0.05):
            ctx.notes.append("stopped early: deadline or budget")
            break
        r = agent.run(request, tenant="eval-drift")
        ctx.spent_usd += r.cost_usd
        attempted += 1
        correct += bool(r.answer) and r.answer["decision"] == expected and r.outcome in ("ok", "fallback")
    accuracy = correct / attempted if attempted else 0.0
    return {"accuracy": round(accuracy, 3), "attempted": attempted, "baseline": baseline_accuracy,
            "drifted": attempted == 0 or accuracy < baseline_accuracy - tolerance}


JOBS = {"backlog-hygiene": backlog_hygiene, "eval-drift": eval_drift}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("job", choices=JOBS)
    p.add_argument("--state", type=Path, default=Path(".state"))
    p.add_argument("--run-key")
    p.add_argument("--max-runtime", type=float, default=900)
    p.add_argument("--budget-usd", type=float, default=1.0)
    a = p.parse_args(argv)
    r = run_job(a.job, JOBS[a.job], a.state, a.run_key, a.max_runtime, a.budget_usd)
    print(json.dumps({"job": a.job, "status": r.status, "run_key": r.run_key, "output": r.output, "error": r.error}))
    if r.status == "failed" or (r.output or {}).get("drifted"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
