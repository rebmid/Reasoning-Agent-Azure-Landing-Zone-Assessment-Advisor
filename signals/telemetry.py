"""Runtime performance telemetry for the assessment engine.

Collects timing, query counts, and subscription coverage metrics
that are embedded in the run JSON for operational visibility.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class RunTelemetry:
    """Accumulates performance metrics throughout a single scan."""

    # Subscription coverage
    subscriptions_visible: int = 0
    subscriptions_total: int = 0        # from MG descendants or equal to visible
    coverage_percent: float = 0.0

    # Resource Graph
    rg_query_count: int = 0
    rg_total_duration_ms: int = 0

    # ARM / per-subscription
    arm_call_count: int = 0
    arm_total_duration_ms: int = 0

    # Signal layer
    signals_fetched: int = 0
    signals_cached: int = 0
    signal_errors: int = 0

    # Overall timing (in seconds)
    phase_context_sec: float = 0.0
    phase_signals_sec: float = 0.0
    phase_evaluators_sec: float = 0.0
    phase_ai_sec: float = 0.0
    phase_reporting_sec: float = 0.0
    assessment_duration_sec: float = 0.0

    # Whether a live scan was actually executed (set by scan.py)
    _live_run: bool = field(default=False, repr=False)

    # Internal timing helpers (not serialized)
    _phase_starts: dict[str, float] = field(default_factory=dict, repr=False)

    def start_phase(self, name: str) -> None:
        self._phase_starts[name] = time.perf_counter()

    def end_phase(self, name: str) -> None:
        start = self._phase_starts.pop(name, None)
        if start is not None:
            elapsed = round(time.perf_counter() - start, 2)
            attr = f"phase_{name}_sec"
            if hasattr(self, attr):
                setattr(self, attr, elapsed)

    def record_signal_events(self, events: list[dict[str, Any]]) -> None:
        """Ingest SignalBus events to populate query and cache counters."""
        for ev in events:
            etype = ev.get("type", "")
            if etype == "signal_returned":
                if ev.get("cache_hit"):
                    self.signals_cached += 1
                else:
                    self.signals_fetched += 1
                    ms = ev.get("ms", 0) or 0
                    signal = ev.get("signal", "")
                    if signal.startswith("resource_graph:"):
                        self.rg_query_count += 1
                        self.rg_total_duration_ms += ms
                    else:
                        self.arm_call_count += 1
                        self.arm_total_duration_ms += ms
            elif etype == "signal_error":
                self.signal_errors += 1

    def mark_live(self) -> None:
        """Mark this telemetry instance as belonging to a live scan."""
        self._live_run = True

    @property
    def is_live(self) -> bool:
        return self._live_run

    def to_dict(self) -> dict[str, Any]:
        """Serialize telemetry for inclusion in run JSON.

        Only includes metric fields that were actually populated
        during a live scan.  If no live scan ran, the dict will
        contain only ``live_run: false`` so downstream consumers
        can distinguish 'never collected' from 'collected zero'.
        """
        d = asdict(self)
        d.pop("_phase_starts", None)
        d["live_run"] = d.pop("_live_run", False)
        if not d["live_run"]:
            # Strip numeric fields — they are uninitialised defaults
            return {"live_run": False}
        return d

    def summary_lines(self) -> list[str]:
        """Human-readable summary for terminal output."""
        lines = [
            f"  Subscriptions:    {self.subscriptions_visible} visible"
            f" / {self.subscriptions_total} total"
            f" ({self.coverage_percent}% coverage)",
            f"  RG queries:       {self.rg_query_count}"
            f"  ({self.rg_total_duration_ms}ms)",
            f"  ARM calls:        {self.arm_call_count}"
            f"  ({self.arm_total_duration_ms}ms)",
            f"  Signals:          {self.signals_fetched} fetched,"
            f" {self.signals_cached} cached,"
            f" {self.signal_errors} errors",
            f"  Phases:           context={self.phase_context_sec}s"
            f"  signals={self.phase_signals_sec}s"
            f"  evaluators={self.phase_evaluators_sec}s"
            f"  ai={self.phase_ai_sec}s"
            f"  reporting={self.phase_reporting_sec}s",
            f"  Total duration:   {self.assessment_duration_sec}s",
        ]
        return lines
