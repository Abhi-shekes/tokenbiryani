"""Persisted usage history, and the bucketing that turns it into charts.

The event log (`events.py`) is a 500-entry ring in memory: perfect for "what is
happening right now", useless for "what did last Tuesday cost". Charts need the
second question answered, and answering it means writing a row per request and
keeping it.

Two decisions worth stating.

**One row per request, like the spend ledger.** Not a pre-aggregated rollup. A
rollup has to choose its buckets at write time, and every question you did not
anticipate — cost by model, cache rate for one account, errors in a five minute
window — becomes unanswerable. Rows are cheap and the retention window is bounded.

**Aggregation lives here, not in the backends.** Each store only has to append a
row and hand back the rows in a window; the bucketing, the grouping and every
derived rate are computed once, in `aggregate`. Three backends implementing the
same arithmetic three times is three chances to disagree, and a chart that differs
by backend is worse than no chart.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: What a stored row holds. Deliberately flat and JSON-safe: SQLite gets columns,
#: Redis and memory get the dict verbatim.
USAGE_FIELDS = (
    "at",
    "account_id",
    "key_name",
    "model",
    "status",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "latency",
    "affinity_broken",
    "via",
    "error",
)

#: How a `group_by` name maps onto a row field.
GROUP_FIELDS = {
    "account": "account_id",
    "model": "model",
    "key": "key_name",
}

#: Windows the console offers, in seconds.
WINDOWS = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}

#: A readable bucket for each, chosen so a chart lands between 12 and 60 marks.
DEFAULT_BUCKETS = {3600: 300, 86400: 3600, 604800: 21600, 2592000: 86400}


def sample_from_event(event: Any) -> Dict[str, Any]:
    """Flatten a `RequestEvent` into a storable row.

    Failed requests are recorded too — a chart that only shows successes hides the
    outage, which is the thing you opened the chart to see.
    """
    return {
        "at": float(event.started_at),
        "account_id": event.account_id or "",
        "key_name": event.key_name or "",
        "model": event.model or "",
        "status": int(event.status) if event.status is not None else 0,
        "input_tokens": int(event.input_tokens or 0),
        "output_tokens": int(event.output_tokens or 0),
        "cache_read_tokens": int(event.cache_read_tokens or 0),
        "cache_creation_tokens": int(event.cache_creation_tokens or 0),
        "cost_usd": float(event.cost_usd) if event.cost_usd is not None else 0.0,
        "latency": float(event.latency or 0.0),
        "affinity_broken": 1 if event.affinity_broken else 0,
        "via": event.via or "messages",
        "error": event.error or "",
    }


def resolve_window(raw: Optional[str], fallback: int = 86400) -> int:
    """Accept either a label the console sends (`24h`) or a raw second count."""
    if raw is None:
        return fallback
    text = str(raw).strip().lower()
    if text in WINDOWS:
        return WINDOWS[text]
    try:
        seconds = int(float(text))
    except ValueError:
        return fallback
    # A window of a year of per-request rows is a denial of service on yourself.
    return max(60, min(seconds, WINDOWS["30d"]))


def resolve_bucket(window_seconds: int, raw: Optional[str]) -> int:
    if raw is not None:
        try:
            requested = int(float(raw))
        except ValueError:
            requested = 0
        if requested > 0:
            # Never fewer than 2 or more than 240 marks: past that a line chart is
            # either a single dot or an unreadable comb.
            return max(window_seconds // 240, min(requested, window_seconds // 2))
    for size, bucket in sorted(DEFAULT_BUCKETS.items()):
        if window_seconds <= size:
            return bucket
    return DEFAULT_BUCKETS[2592000]


def _blank() -> Dict[str, Any]:
    return {
        "requests": 0,
        "errors": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.0,
        "cache_breaks": 0,
        "_latency_sum": 0.0,
    }


def _add(cell: Dict[str, Any], row: Dict[str, Any]) -> None:
    cell["requests"] += 1
    status = int(row.get("status") or 0)
    if row.get("error") or status == 0 or status >= 400:
        cell["errors"] += 1
    cell["input_tokens"] += int(row.get("input_tokens") or 0)
    cell["output_tokens"] += int(row.get("output_tokens") or 0)
    cell["cache_read_tokens"] += int(row.get("cache_read_tokens") or 0)
    cell["cache_creation_tokens"] += int(row.get("cache_creation_tokens") or 0)
    cell["cost_usd"] += float(row.get("cost_usd") or 0.0)
    cell["cache_breaks"] += 1 if row.get("affinity_broken") else 0
    cell["_latency_sum"] += float(row.get("latency") or 0.0)


def _finalise(cell: Dict[str, Any]) -> Dict[str, Any]:
    """Turn running sums into the derived rates a chart actually plots."""
    billed = (
        cell["input_tokens"] + cell["cache_read_tokens"] + cell["cache_creation_tokens"]
    )
    requests = cell["requests"]
    out = {k: v for k, v in cell.items() if not k.startswith("_")}
    out["billed_input_tokens"] = billed
    out["total_tokens"] = billed + cell["output_tokens"]
    out["cost_usd"] = round(cell["cost_usd"], 6)
    # None, not 0.0: "no traffic" and "every request missed the cache" are different
    # answers, and a chart that draws them the same is lying about the quiet hours.
    out["cache_hit_rate"] = (
        round(cell["cache_read_tokens"] / billed, 4) if billed else None
    )
    out["latency_avg"] = round(cell["_latency_sum"] / requests, 4) if requests else None
    return out


def aggregate(
    rows: Iterable[Dict[str, Any]],
    now: float,
    window_seconds: int,
    bucket_seconds: int,
    group_by: str = "account",
) -> Dict[str, Any]:
    """Bucket raw usage rows into a time series plus totals.

    Buckets are emitted for the whole window even when empty, so a gap in traffic
    draws as a gap rather than silently compressing the time axis.
    """
    field = GROUP_FIELDS.get(group_by, "account_id")
    count = max(1, int(window_seconds // bucket_seconds))
    # Anchor to the bucket grid so the last bucket is the live, partial one and
    # every earlier bucket covers a full period.
    end = (int(now) // bucket_seconds) * bucket_seconds + bucket_seconds
    start = end - count * bucket_seconds

    buckets: List[Dict[str, Any]] = [
        {"at": start + index * bucket_seconds, "_all": _blank(), "_by": {}}
        for index in range(count)
    ]
    totals: Dict[str, Dict[str, Any]] = {}
    overall = _blank()

    for row in rows:
        at = float(row.get("at") or 0.0)
        if at < start or at >= end:
            continue
        index = int((at - start) // bucket_seconds)
        if index < 0 or index >= count:
            continue
        name = str(row.get(field) or "—")
        bucket = buckets[index]
        _add(bucket["_all"], row)
        if name not in bucket["_by"]:
            bucket["_by"][name] = _blank()
        _add(bucket["_by"][name], row)
        if name not in totals:
            totals[name] = _blank()
        _add(totals[name], row)
        _add(overall, row)

    # Largest first: the legend order and the stacking order should agree, and the
    # series worth reading should be the one nearest the axis.
    order = sorted(totals, key=lambda n: -totals[n]["input_tokens"] - totals[n]["output_tokens"])

    return {
        "window_seconds": window_seconds,
        "bucket_seconds": bucket_seconds,
        "group_by": group_by,
        "start": start,
        "end": end,
        "groups": order,
        "buckets": [
            {
                "at": bucket["at"],
                "all": _finalise(bucket["_all"]),
                "by": {name: _finalise(cell) for name, cell in bucket["_by"].items()},
            }
            for bucket in buckets
        ],
        "totals": {name: _finalise(cell) for name, cell in totals.items()},
        "overall": _finalise(overall),
    }
