# Spending the week on purpose

Every other number in this gateway answers *is there capacity right now*. None of
them answer *should this be spent now*, and those are different questions.

A pool that burns its week by Wednesday and a pool that reaches Sunday with a third
of its quota unused are both failures. Neither shows up in a headroom meter, because
headroom is full again after every reset — right up until the window it belongs to
runs out.

`GET /admin/pacing` answers the second question.

## Two signals, not interchangeable

**A subscription reports its own window.** `anthropic-ratelimit-unified-7d-*` carries
utilisation and a reset timestamp, so the pace is *measured*. This is the good path
and the one this feature was built for.

**An API key has no weekly window at all.** Its limits are per-minute and no header
says anything about a week. So pacing there compares spend since the start of the
calendar week against a budget you state:

```yaml
pacing:
  weekly_budget_usd: 400
```

State no budget and API-key accounts are simply not paced. Inventing a weekly limit
would be a number nobody can attribute — the same reason this project ships no price
list.

## What a reading says

```json
{
  "scope": "acct-01",
  "source": "unified",
  "elapsed_fraction": 0.5,
  "target": 0.5,
  "utilization": 0.9,
  "pace": 0.4,
  "projected_utilization": 1.8,
  "stranded_fraction": 0.0,
  "exhausted_in_seconds": 46666.6,
  "verdict": "ahead of pace — at this rate the quota is gone in 13.0h, with 3.5d of the window still to go"
}
```

`pace` is `utilization - target`. Positive is ahead — on course to run dry early.
Negative is behind — on course to strand quota. The pool's reading is the *tightest*
of them, not the mean: one account about to run dry on Wednesday is the fact worth
acting on, and averaging it against three healthy ones is how that fact gets lost.

## The curve

A linear target expects a fifth of the quota spent over a weekend. For a team that
works Monday to Friday that is wrong twice: it reads as *behind pace* every Monday
morning and *ahead of pace* every Friday afternoon.

```yaml
pacing:
  curve: business_hours   # Mon-Fri 09:00-17:00 UTC
```

## Enforcing

Advisory is the default: it reports, and changes nothing.

```yaml
pacing:
  mode: enforcing
  ahead_threshold: 0.10
  max_batch_delay_seconds: 30
```

Enforcement holds **`batch`-priority requests only**, for a delay that scales with how
far ahead of pace the pool is, up to `max_batch_delay_seconds`.

**Interactive traffic is never delayed.** There is no threshold at which making
someone's session slower is the right way to hit a budget figure — an operator who
wants that wants a spend cap, which already exists and fails honestly instead of
quietly adding latency.

It is a wait rather than a rejection, deliberately. Waiting is what spends a window
more slowly; rejecting just moves the same work to whenever the client retries, which
is usually immediately. The wait is bounded by that request's own deadline and wait
budget like any other, and shows up as `paced_for` on the request.

## Acting on it, beyond waiting

Once pacing knows the pool is ahead, holding batch work back is not the only lever.

### The cheaper lane

```yaml
pacing:
  prefer_batch_lane_when_ahead: true   # the default, needs batch.enabled
```

While ahead of pace, batch-priority work goes to the Message Batches API **even
though the pool has capacity for it now**. Batches are priced below standard and
spend a different upstream limit, so this is strictly better than waiting: the work
still happens, and it costs less. It is tried before the delay for that reason.

With `batch.enabled` off there is no lane to prefer and the setting does nothing.

### Downshifting the model

```yaml
pacing:
  model_downshift:
    claude-opus-*: claude-sonnet-5
```

**Empty by default, and think before filling it in.** Every other lever here changes
*when* or *where* a request runs. This one changes *what the caller gets*, which is a
different kind of decision and not one a gateway should make quietly.

Three bounds, all enforced:

- **Batch priority only.** An interactive session is never downshifted.
- **Never widens a key's reach.** A substitution into a model the key's `models` list
  does not allow is refused — a pacing policy must not hand a tenant something their
  key does not permit.
- **Recorded.** The request carries `model_requested` alongside `model`, and the
  inspector shows both.

One interaction worth knowing: the model is part of the affinity fingerprint, so a
conversation that downshifts mid-flight lands on a different session key and takes a
cache break. That is another reason this is batch-only — batch traffic is usually
single-turn, where there is no cache to lose.

## Caveats worth knowing

- The unified windows are **rolling**, and pacing treats one as though it began
  `window_seconds` before it resets. That is an approximation — but an approximation
  over a measured utilisation figure, which is not the same as a guess.
- The calendar week starts **Monday 00:00 UTC**, matching the spend ledger. A week
  starting at a different instant from the ledger it is measured against would be
  wrong twice a year for anyone observing daylight saving.
- Extrapolation needs elapsed time. At the very start of a window nothing is
  projected, because turning the first request into "you run dry today" is worse than
  saying nothing.
