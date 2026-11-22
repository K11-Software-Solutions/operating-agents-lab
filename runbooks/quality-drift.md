# Runbook: nightly eval drift

**Signal:** the `eval-drift` scheduled job exits non-zero (`drifted: true`).

1. Read the job's result in the state volume (`eval-drift.<date>.json`): accuracy against the
   baseline, how many cases ran, and any notes (stopped at the deadline or the budget).
2. **Nothing changed on our side?** Model behaviour can shift between provider versions. Pin the
   model id if it isn't pinned, and rerun lab 6's full eval.
3. **We changed something?** Lab 7's eval gate should have caught it before release. Add the
   failing cases to the eval set so it does next time.
4. A run that stopped early (deadline or budget) is not drift; fix the schedule or the budget.
