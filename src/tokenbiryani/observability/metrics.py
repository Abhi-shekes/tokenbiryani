"""Prometheus exposition, hand-rolled. Two counters and a gauge don't need a client lib."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Tuple

Labels = Tuple[Tuple[str, str], ...]


def _fmt_labels(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join('{}="{}"'.format(k, str(v).replace('"', '\\"')) for k, v in labels)
    return "{" + inner + "}"


class Metrics:
    def __init__(self) -> None:
        self._counters: Dict[str, Dict[Labels, float]] = defaultdict(lambda: defaultdict(float))
        self._help: Dict[str, Tuple[str, str]] = {}
        self._gauges: List[Tuple[str, str, Callable[[], Iterable[Tuple[Labels, float]]]]] = []

    def declare(self, name: str, help_text: str, kind: str = "counter") -> None:
        self._help[name] = (help_text, kind)

    def incr(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        self._counters[name][key] += value

    def register_gauge(
        self, name: str, help_text: str, source: Callable[[], Iterable[Tuple[Labels, float]]]
    ) -> None:
        self._help[name] = (help_text, "gauge")
        self._gauges.append((name, help_text, source))

    def render(self) -> str:
        lines: List[str] = []
        for name, series in sorted(self._counters.items()):
            help_text, kind = self._help.get(name, ("", "counter"))
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in sorted(series.items()):
                lines.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
        for name, help_text, source in self._gauges:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            for labels, value in source():
                lines.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
        return "\n".join(lines) + "\n"


def _num(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(round(value, 6))
