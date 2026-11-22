# 04. When the Provider Has a Bad Day

*Operating Agents Lab, article 4 of 8*

Every model provider has incidents. The question is what your users see when it happens.

## Layer 1: retry, carefully

```python
retry(fn, attempts=3, base_s=0.2, cap_s=5.0)
```

- **Only retryable errors:** 429, 529, 5xx and timeouts. A 400 means our request is wrong, and
  retrying it just wastes time. A test checks it's tried exactly once.
- **Exponential backoff with full jitter.** Each wait is random between 0 and the cap, so a fleet
  of clients doesn't retry in lockstep.
- **Honour `Retry-After`.** When the provider says when to come back, do what it says.

The Anthropic SDK already retries some errors twice on its own. This layer decides when to give up
on a model entirely.

## Layer 2: the circuit breaker

After repeated failures, keep retrying and you make an outage worse, for you and for the provider.
The breaker opens after N consecutive failures and fails fast for a cool-down period. Then it lets
one probe through: success closes it, failure opens it again. Measured in a 30-minute primary
outage on the simulated clock: 90 requests arrive, retries alone would make 270 calls to the
failing model, and with the breaker it gets 32.

## Layer 3: a fallback model

When the primary model is unavailable, a second model (here, a smaller and cheaper one) answers.
The outcome is `fallback`. It counts as a full-quality answer, so make sure that's true for your
task: the fallback should pass the same eval as the primary, at a lower bar if you set one.

## Layer 4: degraded mode, labelled

If both models are down, the agent answers from lab 6's **rules baseline** and says so: the outcome
is `degraded` and the model is `rules-baseline`. A labelled degraded answer is usually better than
an error. The caller can show "results may be less accurate", and the full-quality SLO counts every
one of them.

## What not to do during an outage

Don't raise retry counts. Don't switch every tenant to a different provider without its eval
passing. Don't silently serve the baseline as if it were the model. The runbook
(`runbooks/provider-outage.md`) lists these.
