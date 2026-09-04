"""The spill lane: one Messages request run through the Message Batches API.

Batches are cheaper but asynchronous. The gateway holds the client's connection
while it polls, bounded by that request's own wait budget — never longer. A batch
that outlives the budget is cancelled and the id is handed back, so nothing is
silently abandoned upstream.

Only `batch` priority spills, and never a streaming request: batches do not stream.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

import httpx

from ..providers.base import Upstream

CUSTOM_ID = "tokenbiryani-spill"

#: Terminal processing_status values from the Batches API.
ENDED = "ended"
TERMINAL = {"ended", "canceled", "cancelled", "expired", "errored"}


class BatchUnavailable(Exception):
    """The spill lane could not serve this request; fall back to queueing."""


class BatchTimeout(Exception):
    """The batch outlived the request's wait budget. Carries the id for follow-up."""

    def __init__(self, batch_id: Optional[str]) -> None:
        super().__init__("batch did not finish within the request's wait budget")
        self.batch_id = batch_id


@dataclass
class BatchOutcome:
    message: Dict[str, Any]
    batch_id: str
    polls: int
    waited: float


def _first_result(payload: bytes) -> Optional[Dict[str, Any]]:
    """Batch results are JSONL. We submit one request, so we read one line."""
    for line in payload.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def run_single(
    client: httpx.AsyncClient,
    upstream: Upstream,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    poll_interval: float,
    deadline: float,
) -> BatchOutcome:
    """Submit one request as a batch and wait for it, within `deadline`."""
    started = time.time()
    payload = {k: v for k, v in params.items() if k != "stream"}

    submitted = await upstream.submit_batch(
        client, [{"custom_id": CUSTOM_ID, "params": payload}], headers
    )
    if submitted.status >= 300 or not submitted.body:
        raise BatchUnavailable(
            f"batch submission returned {submitted.status}"
        )
    batch_id = str(submitted.body.get("id") or "")
    if not batch_id:
        raise BatchUnavailable("batch submission returned no id")

    polls = 0
    status = str(submitted.body.get("processing_status") or "in_progress")
    while status not in TERMINAL:
        if time.time() >= deadline:
            await _cancel(client, upstream, batch_id, headers)
            raise BatchTimeout(batch_id)
        await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.time())))
        polls += 1
        polled = await upstream.poll_batch(client, batch_id, headers)
        if polled.status >= 300 or not polled.body:
            raise BatchUnavailable(f"batch poll returned {polled.status}")
        status = str(polled.body.get("processing_status") or "in_progress")

    if status != ENDED:
        raise BatchUnavailable(f"batch finished as {status}")

    fetched = await upstream.fetch_batch_results(client, batch_id, headers)
    if fetched.status >= 300:
        raise BatchUnavailable(f"batch results returned {fetched.status}")

    entry = _first_result(fetched.raw)
    if entry is None:
        raise BatchUnavailable("batch results were empty")

    result = entry.get("result") or {}
    if result.get("type") != "succeeded":
        raise BatchUnavailable(
            "batch request {}".format(result.get("type") or "did not succeed")
        )
    message = result.get("message")
    if not isinstance(message, dict):
        raise BatchUnavailable("batch result carried no message")

    return BatchOutcome(
        message=message, batch_id=batch_id, polls=polls, waited=time.time() - started
    )


async def _cancel(
    client: httpx.AsyncClient,
    upstream: Upstream,
    batch_id: str,
    headers: Mapping[str, str],
) -> None:
    try:
        await upstream.cancel_batch(client, batch_id, headers)
    except Exception:  # noqa: BLE001 - a failed cancel must not mask the timeout
        pass
