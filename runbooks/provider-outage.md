# Runbook: model provider degraded or down

**Alerts:** `full_quality_burn` (page or ticket), `availability_burn` (page or ticket).

## What the system already did

1. Retried retryable errors (429, 529, 5xx, timeouts) with backoff, honouring `Retry-After`.
2. Opened the circuit breaker after repeated failures, so it stops hammering the provider.
3. Answered from the **fallback model**, then from the **rules baseline** (outcome `degraded`) if
   both were down. Callers got answers marked as degraded, not errors.

## What you do

1. **Confirm.** Check the provider's status page. In traces, filter `invoke_agent` spans by
   `kestrel.outcome` and look at `error.type` on the `chat` spans: `Overloaded`, `RateLimited` or
   `ServerError`.
2. **Is it us?** A sudden rise in `RateLimited` alone usually means our traffic or our limits
   changed, not an outage. Check `kestrel_agent_requests_total` by route and tenant.
3. **Communicate.** If degraded answers last more than 15 minutes, tell users that triage results
   are rules-based until further notice.
4. **Don't** raise retry counts during an outage. Retries multiply load on a struggling provider.
5. **Recovery.** The breaker lets one probe through after its cool-down. Watch `fallback` and
   `degraded` fall back to zero, then close the incident.
