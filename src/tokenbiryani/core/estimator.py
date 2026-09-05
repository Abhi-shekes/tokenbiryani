"""Predicting what a request will actually return, instead of believing its ceiling.

`max_tokens` is the only honest upper bound available before a request runs, which
is why the lease started out using it. It is also wildly wrong in the case this
gateway exists to serve: an agent client sends `max_tokens: 32000` and returns a few
hundred tokens.

The lease is released afterwards, so no quota is *spent* on the difference. The cost
is paid during the request instead. An account whose output window is leased at 32k
reads as full, an account that reads as full is filtered out of routing, and a
conversation whose owner is filtered out gets re-homed onto a credential that has
never seen its prefix. That is how an estimation problem turns into a cache break,
which is the most expensive thing that can happen here.

Measured against a mirrored output window, believing the ceiling costs most of the
pool's concurrency:

    output limit 16,000/window, max_tokens  8,192 ->  1 concurrent request
    output limit 64,000/window, max_tokens 32,000 ->  2 concurrent requests

So: keep a rolling sample of what each model really returns, lease a high quantile
of it, and never lease more than the caller's own ceiling — the prediction can only
ever reserve *less* than today. Fall back to the ceiling until there is enough
evidence to beat it, and again if the prediction turns out to be beaten too often.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Deque, Dict

#: Estimating from fewer samples than this is guessing with extra steps.
DEFAULT_MIN_SAMPLES = 20

#: How many recent completions per model take part. Long enough to be stable across
#: a mix of short and long turns, short enough to follow a change in how a client
#: is being used.
DEFAULT_WINDOW = 200

#: Never predict below this. A model that has only ever answered in one word must
#: not leave a two-word answer under-reserved.
DEFAULT_FLOOR = 256

#: A p95 predictor is beaten about 5% of the time by construction. This is the
#: safety valve for a distribution that has changed shape, not for that.
DEFAULT_MAX_UNDERSHOOT = 0.20

MODE_ADAPTIVE = "adaptive"
MODE_CEILING = "max_tokens"


class OutputEstimator:
    """Per-model output-size prediction, bounded by the caller's own `max_tokens`.

    Shared across the pool rather than held per account: what a model returns is a
    property of the model and the traffic, not of the credential it was billed to,
    and splitting the samples per account would just make each one slower to learn
    the same number.
    """

    def __init__(
        self,
        mode: str = MODE_ADAPTIVE,
        quantile: float = 0.95,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        window: int = DEFAULT_WINDOW,
        floor: int = DEFAULT_FLOOR,
        max_undershoot: float = DEFAULT_MAX_UNDERSHOOT,
    ) -> None:
        self.mode = mode
        self.quantile = min(1.0, max(0.5, float(quantile)))
        self.min_samples = max(1, int(min_samples))
        self.window = max(self.min_samples, int(window))
        self.floor = max(1, int(floor))
        self.max_undershoot = max(0.0, min(1.0, float(max_undershoot)))
        self._samples: Dict[str, Deque[int]] = defaultdict(
            lambda: deque(maxlen=self.window)
        )
        self._undershoots: Dict[str, Deque[bool]] = defaultdict(
            lambda: deque(maxlen=self.window)
        )

    @property
    def enabled(self) -> bool:
        return self.mode == MODE_ADAPTIVE

    def predict(self, model: str, ceiling: int) -> int:
        """What to lease for this request. Never more than `ceiling`.

        Returning `ceiling` is always a valid answer and is what happens whenever
        the estimator has no business having an opinion.
        """
        ceiling = max(1, int(ceiling))
        if not self.enabled:
            return ceiling
        samples = self._samples.get(model)
        if samples is None or len(samples) < self.min_samples:
            return ceiling
        if self._undershoot_rate(model) > self.max_undershoot:
            # The distribution moved. Stop predicting until the record of being
            # beaten ages out of the window.
            return ceiling
        predicted = _quantile(samples, self.quantile)
        return max(self.floor, min(ceiling, predicted))

    def observe(self, model: str, predicted: int, actual: int) -> None:
        """Record one completion: what it returned, and whether it beat the lease."""
        if actual <= 0:
            # No usage came back — an error, or an upstream that reported none.
            # Neither says anything about how long an answer is.
            return
        self._samples[model].append(int(actual))
        self._undershoots[model].append(int(actual) > int(predicted))

    def _undershoot_rate(self, model: str) -> float:
        seen = self._undershoots.get(model)
        if not seen or len(seen) < self.min_samples:
            return 0.0
        return sum(1 for missed in seen if missed) / float(len(seen))

    def snapshot(self) -> Dict[str, Any]:
        """What the estimator currently believes, for the admin API and the console."""
        models: Dict[str, Any] = {}
        for model, samples in self._samples.items():
            if not samples:
                continue
            ordered = sorted(samples)
            models[model] = {
                "samples": len(ordered),
                "predicting": len(ordered) >= self.min_samples
                and self._undershoot_rate(model) <= self.max_undershoot,
                "median": ordered[len(ordered) // 2],
                "p95": _quantile(samples, 0.95),
                "max": ordered[-1],
                "undershoot_rate": round(self._undershoot_rate(model), 4),
            }
        return {
            "mode": self.mode,
            "quantile": self.quantile,
            "min_samples": self.min_samples,
            "floor": self.floor,
            "models": models,
        }


def _quantile(values: Any, fraction: float) -> int:
    """Nearest-rank quantile. No numpy, and no interpolation to argue about."""
    ordered = sorted(values)
    if not ordered:
        return 0
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return int(ordered[index])
