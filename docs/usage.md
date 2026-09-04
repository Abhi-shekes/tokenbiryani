# Usage and charts

The **Usage** screen answers "what has this pool actually spent, and on what" over
1 hour, 24 hours, 7 days or 30 days, grouped by account, model or virtual key.

## Where the numbers come from

Two different stores, and the difference matters.

| | Live feed and Overview | Usage screen |
|---|---|---|
| Source | `observability.events` — a ring buffer in memory | `usage_events` in the state store |
| Size | last 500 requests (`observability.event_buffer`) | 90 days |
| Survives a restart | **no** | **yes**, on `sqlite` and `redis` |

Before this existed, every chart in the console was drawn from the ring buffer, so a
restart erased the history. One row per request is now written alongside the spend
ledger, carrying tokens, cache split, model, key, status, latency and cost.

On the `memory` backend the history is bounded at 50,000 rows and still dies with the
process — that store is "everything disappears on restart" by definition. Use `sqlite`
if you want the charts to mean anything tomorrow.

## Retention

90 days, pruned at startup, the same as the spend ledger. Nothing is aggregated away
first: rows stay raw, so a question nobody anticipated is still answerable.

## The complete-bucket rule

The most recent bucket is always the one still filling. Plotting it draws a cliff on
every chart, every time — an artefact of the clock rather than anything the pool did.
So **the charts show complete buckets only**, while the stat tiles and the totals
table cover the whole window including the partial one. The note under the filter row
says which you are looking at.

The exception is a gateway whose first requests are minutes old: when there is no
complete bucket with traffic in it, the partial one is shown rather than an empty
chart.

## Reading the charts

- **Billed input tokens**, stacked by group. Billed input is
  `input + cache_read + cache_write` — what you are charged for, not the raw prompt.
- **Cost** per bucket, in USD. Only models named under `pricing:` produce a cost; the
  gateway ships no price list.
- **Cache hit rate** — `cache_read / billed_input`. This is the number that decides
  the bill, which is why it gets its own chart and its own colour. A quiet bucket
  reports no rate at all rather than 0%, because "no traffic" and "every request
  missed" are different claims.
- **Totals table.** Every value the charts encode as colour, as a number. It is also
  the accessibility relief for the three light-mode series colours that sit below 3:1
  contrast, so it is not optional decoration.

Series colours are assigned to an entity on first sight and never reassigned, so
changing the range or the grouping cannot repaint a series you have already learned.
Past eight groups, the tail folds into a single "Other" rather than cycling hues that
are indistinguishable under colour-vision deficiency.

## The API behind it

```
GET /admin/usage?window=24h&bucket=3600&group_by=account&account=acct-01
```

`window` takes `1h`, `24h`, `7d`, `30d` or a raw second count; `group_by` is
`account`, `model` or `key`; `account` narrows to one. Returns per-bucket series,
per-group totals and an overall summary. Admin key required.
