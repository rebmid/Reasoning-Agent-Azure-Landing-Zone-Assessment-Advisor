"""Signal validation — coverage reporting, binding checks, and query validation.

Three capabilities:
  1. SIGNAL_REGISTRY: maps every control-pack signal key → signal_bus_name
  2. validate_signal_bindings(): fail-fast if data-driven controls lack evaluation
  3. build_signal_execution_summary(): runtime coverage report
  4. run_validate_signals(): --validate-signals mode (probes without scoring)

Does NOT modify scoring logic or output schema.
"""
from __future__ import annotations

import time
from typing import Any

from control_packs.loader import load_pack, ControlPack
from signals.registry import SIGNAL_PROVIDERS
from signals.types import EvalScope, SignalStatus


# ══════════════════════════════════════════════════════════════════
#  1.  SIGNAL_REGISTRY — pack signal key → signal_bus_name
# ══════════════════════════════════════════════════════════════════

def build_signal_registry(
    pack: ControlPack | None = None,
) -> dict[str, str | None]:
    """Build the canonical mapping of pack signal key → signal_bus_name.

    Every entry in the control pack's signals.json gets one row.
    Non-null ``signal_bus_name`` values must have a matching provider
    in ``SIGNAL_PROVIDERS``.
    """
    if pack is None:
        pack = load_pack("alz", "v1.0")

    registry: dict[str, str | None] = {}
    for key, sig_def in pack.signals.items():
        registry[key] = sig_def.get("signal_bus_name")
    return registry


# ══════════════════════════════════════════════════════════════════
#  2.  Binding validation — fail-fast for unbound data-driven controls
# ══════════════════════════════════════════════════════════════════

class SignalBindingError(Exception):
    """Raised when a data-driven control has no evaluation binding."""


def validate_signal_bindings(
    pack: ControlPack | None = None,
    evaluator_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    """Check that every data-driven control has evaluation logic.

    A control is "data-driven" if it has a non-empty ``required_signals``
    list.  Such controls MUST have:
      - A registered evaluator in ``evaluator_ids``, OR
      - An ``evaluator_module`` reference that can resolve at runtime.

    Also verifies that every signal referenced by a control resolves to
    a concrete ``signal_bus_name`` that exists in ``SIGNAL_PROVIDERS``.

    Returns a list of violation dicts.  If ``fail_fast=True`` in the
    caller, raise ``SignalBindingError`` on any violation.
    """
    if pack is None:
        pack = load_pack("alz", "v1.0")
    if evaluator_ids is None:
        from evaluators.registry import EVALUATORS
        evaluator_ids = set(EVALUATORS.keys())

    signal_registry = build_signal_registry(pack)
    violations: list[dict[str, str]] = []

    for cid, ctrl in pack.controls.items():
        required_sigs = ctrl.get("required_signals", [])
        if not required_sigs:
            continue  # manual / process-only control — skip

        # Check 1: evaluator must exist (match short id OR full_id)
        full_id = ctrl.get("full_id", "")
        has_evaluator = cid in evaluator_ids or full_id in evaluator_ids
        if not has_evaluator:
            violations.append({
                "control_id": cid,
                "name": ctrl.get("name", ""),
                "type": "missing_evaluator",
                "detail": (
                    f"Data-driven control '{cid}' requires signals "
                    f"{required_sigs} but has no registered evaluator"
                ),
            })

        # Check 2: each referenced signal must resolve to a bus name
        for sig_key in required_sigs:
            bus_name = signal_registry.get(sig_key)
            if bus_name is None:
                violations.append({
                    "control_id": cid,
                    "name": ctrl.get("name", ""),
                    "type": "missing_signal_bus_name",
                    "detail": (
                        f"Signal '{sig_key}' referenced by control '{cid}' "
                        f"has no signal_bus_name mapping"
                    ),
                })
            elif bus_name not in SIGNAL_PROVIDERS:
                violations.append({
                    "control_id": cid,
                    "name": ctrl.get("name", ""),
                    "type": "missing_provider",
                    "detail": (
                        f"Signal '{sig_key}' maps to bus name '{bus_name}' "
                        f"but no provider is registered in SIGNAL_PROVIDERS"
                    ),
                })

    return violations


# ══════════════════════════════════════════════════════════════════
#  3.  Signal execution summary — runtime coverage report
# ══════════════════════════════════════════════════════════════════

def build_signal_execution_summary(
    results: list[dict[str, Any]],
    bus_events: list[dict[str, Any]],
    pack: ControlPack | None = None,
) -> dict[str, Any]:
    """Build the signal coverage / execution summary.

    Returns::

        {
            "total_controls": 412,
            "automated_controls": 52,
            "manual_controls": 360,
            "signals_implemented": 34,
            "signals_referenced": 34,
            "signals_missing_implementation": 0,
            "signal_execution_failures": 3,
            "signal_api_errors": [...],
            "reconciliation_ok": True,
        }
    """
    if pack is None:
        pack = load_pack("alz", "v1.0")

    signal_registry = build_signal_registry(pack)

    # Total controls = all results (automated + manual backfill from checklist)
    total_controls = len(results)
    signal_errors = sum(1 for r in results if r.get("status") == "SignalError")
    manual = sum(1 for r in results if r.get("status") == "Manual")
    automated = total_controls - manual - signal_errors

    # Signals referenced = non-null signal_bus_name values in the pack
    referenced_bus_names = {
        v for v in signal_registry.values() if v is not None
    }
    signals_referenced = len(referenced_bus_names)

    # Signals implemented = intersection with SIGNAL_PROVIDERS
    implemented_bus_names = referenced_bus_names & set(SIGNAL_PROVIDERS.keys())
    signals_implemented = len(implemented_bus_names)
    signals_missing = signals_referenced - signals_implemented

    # Signal execution failures (from bus events)
    failures = [
        ev for ev in bus_events
        if ev.get("type") == "signal_error"
    ]
    signal_execution_failures = len(failures)

    # Signal API errors (from results with status == "Error" or "SignalError")
    api_errors = [
        {
            "control_id": r.get("control_id", ""),
            "status": r.get("status", ""),
            "notes": r.get("notes", ""),
        }
        for r in results
        if r.get("status") in ("Error", "SignalError")
    ]

    # Reconciliation: referenced == implemented, zero missing
    reconciliation_ok = (signals_missing == 0)

    return {
        "total_controls": total_controls,
        "automated_controls": automated,
        "manual_controls": manual,
        "signal_error_controls": signal_errors,
        "signals_implemented": signals_implemented,
        "signals_referenced": signals_referenced,
        "signals_missing_implementation": signals_missing,
        "signal_execution_failures": signal_execution_failures,
        "signal_api_errors": api_errors,
        "reconciliation_ok": reconciliation_ok,
    }


def print_signal_execution_summary(summary: dict[str, Any]) -> None:
    """Pretty-print the signal execution summary to terminal."""
    ok = "✅" if summary["reconciliation_ok"] else "❌"
    print("\n┌─ Signal Coverage Report ──────────────────────────────────┐")
    print(f"│  Total controls:                {summary['total_controls']:>6}")
    print(f"│  Automated controls:            {summary['automated_controls']:>6}")
    print(f"│  Manual controls:               {summary['manual_controls']:>6}")
    print(f"│  Signals implemented:           {summary['signals_implemented']:>6}")
    print(f"│  Signals referenced:            {summary['signals_referenced']:>6}")
    print(f"│  Signals missing implementation: {summary['signals_missing_implementation']:>5}")
    print(f"│  Signal execution failures:     {summary['signal_execution_failures']:>6}")
    print(f"│  Signal API errors:             {len(summary['signal_api_errors']):>6}")
    print(f"│  Reconciliation:                 {ok}")
    if summary["signal_api_errors"]:
        print("│  ──────────────────────────────────────────────────────")
        for err in summary["signal_api_errors"][:10]:
            ctrl = err["control_id"][:20]
            note = err["notes"][:50]
            print(f"│    ✗ {ctrl}: {note}")
    print("└───────────────────────────────────────────────────────────┘")


# ══════════════════════════════════════════════════════════════════
#  4.  --validate-signals mode — probe all signals without scoring
# ══════════════════════════════════════════════════════════════════

def run_validate_signals(
    scope: EvalScope,
    pack: ControlPack | None = None,
    *,
    verbose: bool = True,
) -> dict[str, Any]:
    """Execute every signal provider and classify the result.

    Does NOT run evaluators.  Does NOT compute scoring.

    Returns::

        {
            "total_signals": 34,
            "results": [
                {"signal": "resource_graph:vnets", "status": "ok", "ms": 120, "item_count": 5},
                {"signal": "arm:mg_hierarchy", "status": "empty", "ms": 80, "item_count": 0},
                {"signal": "identity:pim_maturity", "status": "permission_denied", ...},
                {"signal": "cost:idle_resources", "status": "not_found", ...},
                {"signal": "...", "status": "error", "error": "..."},
            ],
            "summary": {
                "ok": 28,
                "empty": 3,
                "permission_denied": 1,
                "not_found": 0,
                "error": 2,
            },
            "binding_violations": [...],
        }
    """
    from signals.registry import SignalBus

    if pack is None:
        pack = load_pack("alz", "v1.0")

    # Build the registry and validate bindings first
    signal_registry = build_signal_registry(pack)
    binding_violations = validate_signal_bindings(pack)

    bus = SignalBus()
    bus_names = sorted({v for v in signal_registry.values() if v is not None})

    probe_results: list[dict[str, Any]] = []
    counts = {"ok": 0, "empty": 0, "permission_denied": 0, "not_found": 0, "error": 0}

    if verbose:
        print("\n╔══════════════════════════════════════════════════════════╗")
        print("║   Signal Validation Mode                                 ║")
        print("╠══════════════════════════════════════════════════════════╣")
        print(f"║  Probing {len(bus_names)} signal providers …{'':>{34 - len(str(len(bus_names)))}}")
        print("╚══════════════════════════════════════════════════════════╝")

    for bus_name in bus_names:
        start_ms = time.perf_counter_ns()
        try:
            result = bus.fetch(bus_name, scope)
            elapsed = (time.perf_counter_ns() - start_ms) // 1_000_000

            # Classify result
            if result.status == SignalStatus.ERROR:
                error_lower = (result.error_msg or "").lower()
                if "403" in error_lower or "forbidden" in error_lower or "authorization" in error_lower:
                    status_class = "permission_denied"
                elif "404" in error_lower or "not found" in error_lower:
                    status_class = "not_found"
                else:
                    status_class = "error"
                entry = {
                    "signal": bus_name,
                    "status": status_class,
                    "ms": elapsed,
                    "item_count": 0,
                    "error": result.error_msg[:200],
                }
            elif result.status == SignalStatus.NOT_AVAILABLE:
                status_class = "not_found"
                entry = {
                    "signal": bus_name,
                    "status": status_class,
                    "ms": elapsed,
                    "item_count": 0,
                    "error": result.error_msg[:200] if result.error_msg else "Not available",
                }
            else:
                # OK — but might be empty
                items = len(result.items) if result.items else 0
                if items == 0 and not result.raw:
                    status_class = "empty"
                else:
                    status_class = "ok"
                entry = {
                    "signal": bus_name,
                    "status": status_class,
                    "ms": elapsed,
                    "item_count": items,
                }

        except Exception as exc:
            elapsed = (time.perf_counter_ns() - start_ms) // 1_000_000
            status_class = "error"
            entry = {
                "signal": bus_name,
                "status": "error",
                "ms": elapsed,
                "item_count": 0,
                "error": str(exc)[:200],
            }

        counts[status_class] += 1
        probe_results.append(entry)

        if verbose:
            icon = {
                "ok": "✅",
                "empty": "⚠️ ",
                "permission_denied": "🔒",
                "not_found": "🚫",
                "error": "❌",
            }.get(status_class, "  ")
            short = bus_name.split(":")[-1] if ":" in bus_name else bus_name
            ms_str = f"{elapsed}ms"
            items_str = f"{entry.get('item_count', 0)} items" if status_class in ("ok", "empty") else ""
            err_str = entry.get("error", "")[:60] if status_class not in ("ok", "empty") else ""
            detail = items_str or err_str
            print(f"  {icon}  {short:<35} {ms_str:>8}  {detail}")

    report = {
        "total_signals": len(bus_names),
        "results": probe_results,
        "summary": counts,
        "binding_violations": [v for v in binding_violations],
    }

    if verbose:
        print(f"\n┌─ Validation Summary ─────────────────────────────────────┐")
        print(f"│  Total signals probed:    {len(bus_names):>4}")
        print(f"│  OK:                      {counts['ok']:>4}")
        print(f"│  Empty (0 items):         {counts['empty']:>4}")
        print(f"│  Permission denied:       {counts['permission_denied']:>4}")
        print(f"│  Not found (404):         {counts['not_found']:>4}")
        print(f"│  Error:                   {counts['error']:>4}")
        if binding_violations:
            print(f"│  ─────────────────────────────────────────────────────")
            print(f"│  Binding violations:      {len(binding_violations):>4}")
            for v in binding_violations[:5]:
                print(f"│    ✗ {v['control_id']}: {v['type']}")
        print(f"└──────────────────────────────────────────────────────────┘")

    return report
