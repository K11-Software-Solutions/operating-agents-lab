# 07. Scheduled Agents

*Operating Agents Lab, article 7 of 8*

Some agents don't answer requests; they run on a timer. Kestrel has two:

- `backlog-hygiene`, on weekdays, finds stories that look like duplicates;
- `eval-drift`, nightly, re-runs a small eval through the live agent and flags a drop before users
  notice.

Nobody is watching when they run, so they need guard rails that request-serving code gets from a
person at the other end.

## What goes wrong, and the guard

| Failure | Guard in `run_job` |
|---|---|
| the scheduler retries a job that already succeeded | **idempotent run key** (the UTC date by default); a completed key is skipped |
| a slow run overlaps the next | **exclusive lock file**; a second run is refused |
| a run crashes and leaves its lock | a lock older than the deadline is **stale** and broken |
| a run never ends | **deadline**: the job checks `ctx.time_left()` and stops cleanly |
| a run spends all night | **budget**: `ctx.can_spend()` before each model call |
| a crash leaves half a result | **atomic write**: temp file, then rename |
| a failed run blocks the next | a failure writes **no result**, so a retry runs again |

Each row has a test.

## A run that did nothing is not a pass

If `eval-drift` hits its budget before running a single case, it reports `drifted: true` and exits
non-zero. A drift check that silently skips is worse than none. Degraded answers also don't count
as correct: if the model is down at 3 a.m., the nightly eval says so.

## Kubernetes CronJobs

`deploy/cronjobs.yaml` sets:

- `concurrencyPolicy: Forbid`, so Kubernetes won't start a second run (the lock file is a second
  line of defence);
- `startingDeadlineSeconds`, so a run missed while the scheduler was down still happens within the
  hour;
- `activeDeadlineSeconds`, a hard kill above the job's own deadline;
- the same hardening as lab 8's pods: non-root, read-only root filesystem, all capabilities
  dropped.

The state directory is a persistent volume, so run keys and results survive between runs.
