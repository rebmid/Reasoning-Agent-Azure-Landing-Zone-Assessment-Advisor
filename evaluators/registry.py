"""Evaluator registry — maps control GUIDs to ControlEvaluator instances.

Usage:
    from evaluators.registry import EVALUATORS, evaluate_control
    result = evaluate_control("e6c4cfd3-...", scope, signal_bus)
"""
from __future__ import annotations

import time
from typing import Any, Protocol

from signals.types import ControlResult, EvalContext, EvalScope, SignalResult, SignalStatus
from signals.registry import SignalBus


# ── Protocol all evaluators implement ─────────────────────────────
class ControlEvaluator(Protocol):
    control_id: str
    required_signals: list[str]

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult: ...


# ── Registry ──────────────────────────────────────────────────────
EVALUATORS: dict[str, ControlEvaluator] = {}


def register_evaluator(evaluator: ControlEvaluator) -> ControlEvaluator:
    """Register an evaluator instance by its control_id."""
    EVALUATORS[evaluator.control_id] = evaluator
    return evaluator


# ── On-demand control evaluation API ──────────────────────────────
def evaluate_control(
    control_id: str,
    scope: EvalScope,
    bus: SignalBus,
    *,
    run_id: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Evaluate a single control on demand.

    Returns the full API response contract:
    {
      "control_id", "status", "severity", "confidence",
      "evidence", "reason", "signals_used", "next_checks",
      "telemetry": {"duration_ms", "cache_hit"}
    }
    """
    start = time.perf_counter_ns()
    opts = options or {}

    evaluator = EVALUATORS.get(control_id)
    if evaluator is None:
        return {
            "control_id": control_id,
            "status": "Unknown",
            "severity": "Medium",
            "confidence": "Low",
            "evidence": [],
            "reason": f"No evaluator registered for control {control_id}",
            "signals_used": [],
            "next_checks": [],
            "telemetry": {"duration_ms": 0, "cache_hit": False},
        }

    # Fetch required signals (cache-aware)
    # NOTE: do NOT reset_events() — events accumulate for scan-level
    # telemetry.  We snapshot the length to inspect only *this* control's
    # events for the per-control cache_hit flag.
    events_before = len(bus.events)
    freshness = opts.get("freshness_seconds")
    signal_bundle = {}
    for sig_name in evaluator.required_signals:
        signal_bundle[sig_name] = bus.fetch(
            sig_name, scope, freshness_seconds=freshness
        )

    # ── SignalError gate: if ALL signals errored, emit SignalError ─
    # This is distinct from evaluation logic errors (status=Error).
    all_errored = signal_bundle and all(
        s.status in (SignalStatus.ERROR, SignalStatus.SIGNAL_ERROR)
        for s in signal_bundle.values()
    )
    if all_errored:
        ms = (time.perf_counter_ns() - start) // 1_000_000
        error_msgs = "; ".join(
            f"{s.signal_name}: {s.error_msg}" for s in signal_bundle.values() if s.error_msg
        )
        return {
            "control_id": control_id,
            "status": "SignalError",
            "severity": evaluator.__dict__.get("severity", "Medium"),
            "confidence": "Low",
            "confidence_score": 0.0,
            "evidence": [],
            "reason": f"All required signals failed: {error_msgs[:300]}",
            "signals_used": list(signal_bundle.keys()),
            "next_checks": [],
            "coverage": None,
            "telemetry": {"duration_ms": ms, "cache_hit": False},
        }

    # Evaluate deterministically
    ctx = EvalContext(scope=scope, run_id=run_id, options=opts)
    result = evaluator.evaluate(ctx, signal_bundle)

    ms = (time.perf_counter_ns() - start) // 1_000_000
    my_events = bus.events[events_before:]
    cache_hit = all(e.get("cache_hit", False) for e in my_events if e["type"] == "signal_returned")

    return {
        "control_id": control_id,
        "status": result.status,
        "severity": result.severity,
        "confidence": result.confidence,
        "confidence_score": result.confidence_score,
        "evidence": result.evidence,
        "reason": result.reason,
        "signals_used": result.signals_used or list(signal_bundle.keys()),
        "next_checks": result.next_checks,
        "coverage": result.coverage.to_dict() if result.coverage else None,
        "telemetry": {"duration_ms": ms, "cache_hit": cache_hit},
    }


def evaluate_many(
    control_ids: list[str],
    scope: EvalScope,
    bus: SignalBus,
    *,
    run_id: str = "",
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate multiple controls, benefiting from shared signal cache."""
    return [
        evaluate_control(cid, scope, bus, run_id=run_id, options=options)
        for cid in control_ids
    ]
