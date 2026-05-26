import logging
from collections import deque
from typing import Deque, Optional

from .config import settings

log = logging.getLogger(__name__)


class StepEventDetector:
    """Detects on/off step events in total active power.

    Strategy: keep two rolling windows of recent samples — a 'pre' window that
    represents the baseline before a possible step, and a 'post' window that
    captures the level after the step has settled. When the gap between their
    medians exceeds the threshold and the post window is stable, emit an event.
    """

    def __init__(self) -> None:
        self._buf: Deque[dict] = deque()
        self._last_event_ts: float = 0.0
        self._window_s = settings.step_window_s
        self._settle_s = settings.settle_window_s
        self._threshold = settings.step_threshold_w

    def push(self, sample: dict) -> Optional[dict]:
        self._buf.append(sample)
        # Drop samples older than 2 * (window + settle)
        ts = sample["ts"]
        horizon = ts - 2 * (self._window_s + self._settle_s)
        while self._buf and self._buf[0]["ts"] < horizon:
            self._buf.popleft()

        if len(self._buf) < 6:
            return None

        # We need: pre-window (older), gap (settle), post-window (recent)
        recent_cutoff = ts - self._window_s
        gap_cutoff = recent_cutoff - self._settle_s
        pre_cutoff = gap_cutoff - self._window_s

        pre = [s for s in self._buf if pre_cutoff <= s["ts"] < gap_cutoff]
        post = [s for s in self._buf if s["ts"] >= recent_cutoff]

        if len(pre) < 2 or len(post) < 2:
            return None

        pre_power = _median([s["total_power"] or 0.0 for s in pre])
        post_power = _median([s["total_power"] or 0.0 for s in post])
        delta = post_power - pre_power

        if abs(delta) < self._threshold:
            return None

        # Stability check: post window should be tight
        post_powers = [s["total_power"] or 0.0 for s in post]
        if _spread(post_powers) > self._threshold * 0.6:
            return None

        # Debounce: ignore if we just emitted an event
        if ts - self._last_event_ts < self._settle_s:
            return None

        self._last_event_ts = ts

        def pavg(field: str) -> float:
            return _median([s.get(field) or 0.0 for s in post]) - _median([s.get(field) or 0.0 for s in pre])

        event = {
            "ts": ts,
            "direction": "on" if delta > 0 else "off",
            "delta_power": delta,
            "delta_a_power": pavg("a_power"),
            "delta_b_power": pavg("b_power"),
            "delta_c_power": pavg("c_power"),
            "delta_a_current": pavg("a_current"),
            "delta_b_current": pavg("b_current"),
            "delta_c_current": pavg("c_current"),
            "pf_after": _median([s.get("a_pf") or 0.0 for s in post]),
        }
        log.info("Step event: %s %.0fW (A=%.0f B=%.0f C=%.0f)",
                 event["direction"], delta,
                 event["delta_a_power"], event["delta_b_power"], event["delta_c_power"])
        return event


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return xs[n // 2]
    return 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def _spread(xs) -> float:
    if not xs:
        return 0.0
    return max(xs) - min(xs)
