"""Spending a quota window on purpose instead of by accident.

Everything else here answers "is there capacity right now". Nothing answers "should
this be spent now", and those are different questions. A pool that burns its week by
Wednesday and one that reaches Sunday with a third of the quota unused are both
failures, and neither shows up in a headroom meter — headroom is full again after
every reset, right up until the window it belongs to runs out.

Two signals, and they are not interchangeable:

**A subscription reports its own window.** `anthropic-ratelimit-unified-7d-*` carries
utilisation and a reset, so the pace is measured rather than declared. This is the
good path and the one this feature was built for.

**An API key has no weekly window at all.** Its limits are per-minute, and no header
says anything about a week. So pacing there compares spend since the start of the
calendar week against a budget the operator states, and if they state none there is
no pacing — an invented weekly limit would be a number nobody can attribute, which
is the same reason this project ships no price list.

Advisory by default. Enforcement throttles `batch` priority only: degrading someone's
interactive session to hit a budget figure is a worse outcome than missing the figure,
and an operator who wanted that could set a spend cap instead.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

WINDOW_SECONDS = {"5h": 5 * 3600.0, "7d": 7 * 24 * 3600.0}

SOURCE_UNIFIED = "unified"
SOURCE_BUDGET = "budget"

MODE_ADVISORY = "advisory"
MODE_ENFORCING = "enforcing"

CURVE_LINEAR = "linear"
CURVE_BUSINESS_HOURS = "business_hours"

#: Business-hours curve: Monday to Friday, 09:00–17:00 UTC.
_BUSINESS_START_HOUR = 9
_BUSINESS_END_HOUR = 17
_BUSINESS_DAYS = 5
_BUSINESS_HOURS_PER_DAY = _BUSINESS_END_HOUR - _BUSINESS_START_HOUR
_BUSINESS_HOURS_PER_WEEK = _BUSINESS_DAYS * _BUSINESS_HOURS_PER_DAY


def week_start(now: float) -> float:
    """The most recent Monday 00:00 UTC.

    UTC and not the operator's zone, because the spend ledger is in UTC and a week
    that starts at a different instant from the ledger it is measured against would
    be wrong twice a year for anyone observing daylight saving.
    """
    moment = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - _dt.timedelta(days=midnight.weekday())).timestamp()


def business_hours_fraction(now: float) -> float:
    """How much of the working week has passed, as a fraction of its business hours.

    A linear target is wrong for a team that works Monday to Friday: it expects a
    fifth of the quota spent over a weekend when nobody is working, and then reads
    as "behind pace" every Monday morning.
    """
    start = week_start(now)
    elapsed_hours = 0.0
    for day in range(_BUSINESS_DAYS):
        day_start = start + day * 86400.0
        open_at = day_start + _BUSINESS_START_HOUR * 3600.0
        close_at = day_start + _BUSINESS_END_HOUR * 3600.0
        if now >= close_at:
            elapsed_hours += _BUSINESS_HOURS_PER_DAY
        elif now > open_at:
            elapsed_hours += (now - open_at) / 3600.0
    return min(1.0, elapsed_hours / _BUSINESS_HOURS_PER_WEEK)


@dataclass
class PaceReading:
    """One scope's answer to "should this be spent now"."""

    scope: str
    source: str
    window: str
    #: How far through the window we are, 0..1
    elapsed_fraction: float
    #: How much of the quota *should* be gone by now, under the chosen curve
    target: float
    #: How much of it actually is
    utilization: float
    #: utilization - target. Positive is ahead (will run dry early); negative is
    #: behind (will strand quota).
    pace: float
    #: Where utilisation lands at window end if the current rate holds
    projected_utilization: float
    #: Fraction of the window's quota projected to go unused
    stranded_fraction: float
    #: Seconds until the quota is gone at the current rate, when that is before the
    #: window resets
    exhausted_in_seconds: Optional[float]
    verdict: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "source": self.source,
            "window": self.window,
            "elapsed_fraction": round(self.elapsed_fraction, 4),
            "target": round(self.target, 4),
            "utilization": round(self.utilization, 4),
            "pace": round(self.pace, 4),
            "projected_utilization": round(self.projected_utilization, 4),
            "stranded_fraction": round(self.stranded_fraction, 4),
            "exhausted_in_seconds": (
                None if self.exhausted_in_seconds is None
                else round(self.exhausted_in_seconds, 1)
            ),
            "verdict": self.verdict,
        }


class PacingGovernor:
    """Turns utilisation-against-elapsed-time into a pace, and optionally a delay."""

    def __init__(
        self,
        enabled: bool = True,
        mode: str = MODE_ADVISORY,
        window: str = "7d",
        curve: str = CURVE_LINEAR,
        ahead_threshold: float = 0.10,
        behind_threshold: float = 0.10,
        max_batch_delay_seconds: float = 30.0,
        weekly_budget_usd: Optional[float] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.mode = mode
        self.window = window if window in WINDOW_SECONDS else "7d"
        self.curve = curve
        self.ahead_threshold = max(0.0, float(ahead_threshold))
        self.behind_threshold = max(0.0, float(behind_threshold))
        self.max_batch_delay_seconds = max(0.0, float(max_batch_delay_seconds))
        self.weekly_budget_usd = weekly_budget_usd

    def reconfigure(self, **settings: Any) -> None:
        for name, value in settings.items():
            if hasattr(self, name):
                setattr(self, name, value)
        if self.window not in WINDOW_SECONDS:
            self.window = "7d"

    @property
    def window_seconds(self) -> float:
        return WINDOW_SECONDS[self.window]

    @property
    def enforcing(self) -> bool:
        return self.enabled and self.mode == MODE_ENFORCING

    # ---- readings ----------------------------------------------------------------

    def target_for(self, elapsed_fraction: float, now: float) -> float:
        if self.curve == CURVE_BUSINESS_HOURS and self.window == "7d":
            return business_hours_fraction(now)
        return elapsed_fraction

    def from_unified(
        self,
        scope: str,
        utilization: float,
        seconds_to_reset: Optional[float],
        now: float,
    ) -> Optional[PaceReading]:
        """A pace from a subscription's own rolling-window utilisation.

        The window is treated as if it began `window_seconds` before it resets. It
        is really a rolling window rather than a fixed one, so this is an
        approximation — but it is an approximation over a measured utilisation
        figure, which is a different thing from a guess.
        """
        if seconds_to_reset is None:
            return None
        total = self.window_seconds
        elapsed = max(0.0, min(total, total - float(seconds_to_reset)))
        return self._build(
            scope, SOURCE_UNIFIED, elapsed / total, float(utilization),
            float(seconds_to_reset), now,
        )

    def from_budget(
        self, scope: str, spent_usd: float, now: float
    ) -> Optional[PaceReading]:
        """A pace from spend since Monday against a budget the operator stated."""
        budget = self.weekly_budget_usd
        if not budget or budget <= 0:
            return None
        started = week_start(now)
        total = WINDOW_SECONDS["7d"]
        elapsed = max(0.0, min(total, now - started))
        return self._build(
            scope, SOURCE_BUDGET, elapsed / total, spent_usd / float(budget),
            total - elapsed, now,
        )

    def _build(
        self,
        scope: str,
        source: str,
        elapsed_fraction: float,
        utilization: float,
        seconds_remaining: float,
        now: float,
    ) -> PaceReading:
        utilization = max(0.0, utilization)
        target = self.target_for(elapsed_fraction, now)
        pace = utilization - target

        # Extrapolate at the rate established so far. Before anything has elapsed
        # there is no rate to extrapolate, and claiming one would turn the first
        # request of a window into a prediction that the quota runs out today.
        if elapsed_fraction <= 0.0:
            projected = utilization
            exhausted_in: Optional[float] = None
        else:
            rate = utilization / elapsed_fraction
            projected = rate
            if rate > 1.0:
                exhausted_at = 1.0 / rate
                exhausted_in = max(
                    0.0, (exhausted_at - elapsed_fraction) * self._total_for(source)
                )
            else:
                exhausted_in = None

        return PaceReading(
            scope=scope,
            source=source,
            window=self.window if source == SOURCE_UNIFIED else "7d",
            elapsed_fraction=elapsed_fraction,
            target=target,
            utilization=utilization,
            pace=pace,
            projected_utilization=projected,
            stranded_fraction=max(0.0, 1.0 - projected),
            exhausted_in_seconds=exhausted_in,
            verdict=self._verdict(pace, projected, exhausted_in, seconds_remaining),
        )

    def _total_for(self, source: str) -> float:
        return self.window_seconds if source == SOURCE_UNIFIED else WINDOW_SECONDS["7d"]

    def _verdict(
        self,
        pace: float,
        projected: float,
        exhausted_in: Optional[float],
        seconds_remaining: float,
    ) -> str:
        if pace > self.ahead_threshold:
            if exhausted_in is not None:
                return (
                    "ahead of pace — at this rate the quota is gone in "
                    f"{_duration(exhausted_in)}, with {_duration(seconds_remaining)} "
                    "of the window still to go"
                )
            return "ahead of pace, but not on course to run out"
        if pace < -self.behind_threshold:
            stranded = max(0.0, 1.0 - projected)
            return (
                f"behind pace — on course to leave {stranded:.0%} of this window's "
                "quota unused"
            )
        return "on pace"

    # ---- enforcement --------------------------------------------------------------

    def delay_for(self, priority: int, batch_priority: int, pace: Optional[float]) -> float:
        """How long to hold this request back, in seconds. Zero unless enforcing.

        Interactive traffic is never delayed. There is no threshold at which making
        someone's session slower to protect a budget is the right trade — an
        operator who wants that wants a spend cap, which already exists and fails
        honestly instead of quietly adding latency.
        """
        if not self.enforcing or pace is None:
            return 0.0
        if priority < batch_priority:
            return 0.0
        over = pace - self.ahead_threshold
        if over <= 0:
            return 0.0
        # Scale into the delay budget over the same span again, so being twice the
        # threshold ahead holds a batch request for the full allowance.
        share = min(1.0, over / max(self.ahead_threshold, 0.01))
        return round(self.max_batch_delay_seconds * share, 3)

    def report(self, readings: List[PaceReading], now: float) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "window": self.window,
            "curve": self.curve,
            "weekly_budget_usd": self.weekly_budget_usd,
            "week_started_at": week_start(now),
            "readings": [r.to_dict() for r in readings],
        }


def _duration(seconds: float) -> str:
    """Human-sized, because these numbers are read by a person in a console."""
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
