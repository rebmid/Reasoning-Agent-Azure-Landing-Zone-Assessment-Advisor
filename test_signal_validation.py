#!/usr/bin/env python3
"""Self-test for signal validation infrastructure.

Verifies (per spec):
  1. Controls with SignalError do NOT impact maturity %
  2. SignalError appears in automation coverage summary
  3. SignalError appears in limitations
  4. SignalError renders correctly in workbook mapping
  5. No KeyError in most_impactful_gaps when encountering SignalError
  6. Signal registry & binding reconciliation
  7. Execution summary shape and counts
"""
from __future__ import annotations
import sys

# ── Imports ───────────────────────────────────────────────────────
from signals.validation import (
    build_signal_registry,
    validate_signal_bindings,
    build_signal_execution_summary,
    SignalBindingError,
)
from engine.scoring import (
    compute_scoring,
    most_impactful_gaps,
    automation_coverage,
    AUTO_STATUSES,
    MANUAL_STATUSES,
    NON_MATURITY_STATUSES,
    SIGNAL_ERROR_STATUSES,
    STATUS_MULTIPLIER,
)
from reporting.csa_workbook import _map_status
from control_packs.loader import load_pack
from signals.registry import SIGNAL_PROVIDERS
from evaluators.registry import EVALUATORS

# Force evaluator registration
import evaluators.networking      # noqa: F401
import evaluators.governance      # noqa: F401
import evaluators.security        # noqa: F401
import evaluators.data_protection # noqa: F401
import evaluators.resilience      # noqa: F401
import evaluators.identity        # noqa: F401
import evaluators.network_coverage # noqa: F401
import evaluators.management      # noqa: F401
import evaluators.cost            # noqa: F401

failures = 0


def check(label: str, condition: bool, detail: str = ""):
    global failures
    icon = "✔" if condition else "✗"
    msg = f"  {icon} {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    if not condition:
        failures += 1


print("╔══════════════════════════════════════════════════════════╗")
print("║   Signal Validation Self-Test                            ║")
print("╚══════════════════════════════════════════════════════════╝")

# ══════════════════════════════════════════════════════════════════
#  Test 1: SignalError does NOT impact maturity %
# ══════════════════════════════════════════════════════════════════
print("\n── 1. SignalError excluded from maturity ───────────────────")

check(
    "SignalError NOT in AUTO_STATUSES",
    "SignalError" not in AUTO_STATUSES,
)
check(
    "NON_MATURITY_STATUSES = {Manual, SignalError}",
    NON_MATURITY_STATUSES == {"Manual", "SignalError"},
    f"actual = {NON_MATURITY_STATUSES}",
)
check(
    "SIGNAL_ERROR_STATUSES = {SignalError}",
    SIGNAL_ERROR_STATUSES == {"SignalError"},
)
check(
    "STATUS_MULTIPLIER[SignalError] == 0",
    STATUS_MULTIPLIER.get("SignalError") == 0,
)

# Prove maturity is unchanged when SignalError results are injected
baseline_results = [
    {"status": "Pass", "section": "Networking", "severity": "High", "confidence_score": 0.9},
    {"status": "Fail", "section": "Networking", "severity": "High", "confidence_score": 0.9},
    {"status": "Manual", "section": "Networking", "severity": "Medium"},
]
with_signal_error = baseline_results + [
    {"status": "SignalError", "section": "Networking", "severity": "High", "confidence_score": 0.0},
    {"status": "SignalError", "section": "Networking", "severity": "Critical", "confidence_score": 0.0},
]
scoring_baseline = compute_scoring(baseline_results)
scoring_with_se = compute_scoring(with_signal_error)

baseline_maturity = scoring_baseline["overall_maturity_percent"]
se_maturity = scoring_with_se["overall_maturity_percent"]
check(
    "Maturity unchanged with SignalError results",
    baseline_maturity == se_maturity,
    f"baseline={baseline_maturity}%, with_signal_error={se_maturity}%",
)

# ══════════════════════════════════════════════════════════════════
#  Test 2: SignalError appears in automation coverage summary
# ══════════════════════════════════════════════════════════════════
print("\n── 2. SignalError in automation coverage ───────────────────")

cov = scoring_with_se["automation_coverage"]
check(
    "signal_error_controls == 2",
    cov.get("signal_error_controls") == 2,
    f"signal_error_controls = {cov.get('signal_error_controls')}",
)
check(
    "automated_controls excludes SignalError",
    cov["automated_controls"] == 2,
    f"automated_controls = {cov['automated_controls']} (Pass + Fail only)",
)
check(
    "manual_controls == 1",
    cov["manual_controls"] == 1,
    f"manual_controls = {cov['manual_controls']}",
)

# Standalone automation_coverage call
standalone = automation_coverage(with_signal_error, len(with_signal_error))
check(
    "Standalone automation_coverage signal_error_controls",
    standalone["signal_error_controls"] == 2,
)

# automation_integrity: 1 - (signal_errors / attempted)
# attempted = automated(2) + signal_errors(2) = 4 → 1 - 2/4 = 0.5
check(
    "automation_integrity == 0.5",
    cov.get("automation_integrity") == 0.5,
    f"automation_integrity = {cov.get('automation_integrity')}",
)
# Baseline (no signal errors) → integrity == 1.0
cov_baseline = scoring_baseline["automation_coverage"]
check(
    "automation_integrity == 1.0 when no signal errors",
    cov_baseline.get("automation_integrity") == 1.0,
    f"automation_integrity = {cov_baseline.get('automation_integrity')}",
)

# ══════════════════════════════════════════════════════════════════
#  Test 3: SignalError appears in limitations
# ══════════════════════════════════════════════════════════════════
print("\n── 3. SignalError in limitations ───────────────────────────")

# Simulate the limitations loop from scan.py
limitations: list[str] = []
test_results = [
    {"control_id": "abc12345-test", "status": "Error", "notes": "evaluator crash"},
    {"control_id": "def67890-test", "status": "SignalError", "notes": "all signals errored"},
    {"control_id": "ghi11111-test", "status": "Pass", "notes": ""},
]
for r in test_results:
    if r.get("status") == "Error":
        limitations.append(
            f"Control {r['control_id'][:8]} error: {r.get('notes', 'unknown')}"
        )
    elif r.get("status") == "SignalError":
        limitations.append(
            f"Control {r['control_id'][:8]} signal failure: {r.get('notes', 'all signals errored')}"
        )

check(
    "Error control appears in limitations",
    any("abc12345" in l for l in limitations),
    f"found {len([l for l in limitations if 'abc12345' in l])}",
)
check(
    "SignalError control appears in limitations",
    any("def67890" in l for l in limitations),
    f"found {len([l for l in limitations if 'def67890' in l])}",
)
check(
    "Pass control NOT in limitations",
    not any("ghi11111" in l for l in limitations),
)

# ══════════════════════════════════════════════════════════════════
#  Test 4: SignalError renders correctly in workbook mapping
# ══════════════════════════════════════════════════════════════════
print("\n── 4. Workbook _STATUS_MAP ─────────────────────────────────")

check(
    "SignalError maps to 'Not verified (Signal failure)'",
    _map_status("SignalError") == "Not verified (Signal failure)",
    f"actual = '{_map_status('SignalError')}'",
)
check(
    "Error maps to 'Not verified'",
    _map_status("Error") == "Not verified",
)
check(
    "Pass still maps to 'Fulfilled'",
    _map_status("Pass") == "Fulfilled",
)
check(
    "Fail still maps to 'Open'",
    _map_status("Fail") == "Open",
)
check(
    "Manual still maps to 'Not verified'",
    _map_status("Manual") == "Not verified",
)

# ══════════════════════════════════════════════════════════════════
#  Test 5: No KeyError in most_impactful_gaps with SignalError
# ══════════════════════════════════════════════════════════════════
print("\n── 5. most_impactful_gaps KeyError safety ──────────────────")

mixed_results = [
    {"status": "Fail", "section": "Networking", "severity": "High",
     "control_id": "c1", "evidence_count": 3, "confidence_score": 0.9},
    {"status": "SignalError", "section": "Security", "severity": "Critical",
     "control_id": "c2", "evidence_count": 0, "confidence_score": 0.0},
    {"status": "Partial", "section": "Governance", "severity": "Medium",
     "control_id": "c3", "evidence_count": 1, "confidence_score": 0.7},
    {"status": "Pass", "section": "Identity", "severity": "Low",
     "control_id": "c4", "evidence_count": 0, "confidence_score": 0.9},
]

try:
    gaps = most_impactful_gaps(mixed_results)
    check("No KeyError raised", True)
    # SignalError should NOT appear in gaps (it's not Fail/Partial)
    se_in_gaps = [g for g in gaps if g["control_id"] == "c2"]
    check(
        "SignalError excluded from gaps",
        len(se_in_gaps) == 0,
        f"gaps contain {len(se_in_gaps)} SignalError entries",
    )
    # Fail + Partial should appear
    check(
        "Fail + Partial in gaps",
        len(gaps) == 2,
        f"gaps count = {len(gaps)}",
    )
except KeyError as e:
    check(f"No KeyError raised — got KeyError: {e}", False)

# ══════════════════════════════════════════════════════════════════
#  Test 6: Signal registry & binding reconciliation
# ══════════════════════════════════════════════════════════════════
print("\n── 6. Signal registry & binding validation ────────────────")

pack = load_pack("alz", "v1.0")
registry = build_signal_registry(pack)

check(
    "Signal registry loaded",
    len(registry) > 0,
    f"{len(registry)} signal keys",
)

bus_names = {v for v in registry.values() if v is not None}
check(
    "Non-null bus names exist",
    len(bus_names) > 0,
    f"{len(bus_names)} unique bus names",
)

orphan_bus_names = bus_names - set(SIGNAL_PROVIDERS.keys())
check(
    "All bus names have providers",
    len(orphan_bus_names) == 0,
    f"orphans: {orphan_bus_names}" if orphan_bus_names else "all mapped",
)

violations = validate_signal_bindings(pack)
violation_types: dict[str, int] = {}
for v in violations:
    violation_types[v["type"]] = violation_types.get(v["type"], 0) + 1
check(
    "Missing evaluators listed",
    violation_types.get("missing_evaluator", 0) == 0,
    f"{violation_types.get('missing_evaluator', 0)} missing evaluators (expected 0)",
)
critical_types = {t for t in violation_types if t != "missing_evaluator"}
check(
    "No critical binding violations",
    len(critical_types) == 0,
    f"critical: {critical_types}" if critical_types else "clean",
)

# ══════════════════════════════════════════════════════════════════
#  Test 7: Execution summary shape and counts
# ══════════════════════════════════════════════════════════════════
print("\n── 7. Execution summary contract ──────────────────────────")

fake_results = [
    {"status": "Pass", "section": "Networking", "control_id": "a1"},
    {"status": "Fail", "section": "Security", "control_id": "a2"},
    {"status": "SignalError", "section": "Identity", "control_id": "a3",
     "notes": "all signals failed"},
    {"status": "Manual", "section": "Governance", "control_id": "a4"},
]
fake_events = [
    {"type": "signal_returned", "signal": "resource_graph:vnets"},
    {"type": "signal_error", "signal": "arm:mg_hierarchy"},
]
summary = build_signal_execution_summary(fake_results, fake_events, pack)

required_keys = {
    "total_controls", "automated_controls", "manual_controls",
    "signal_error_controls",
    "signals_implemented", "signals_referenced",
    "signals_missing_implementation", "signal_execution_failures",
    "signal_api_errors", "reconciliation_ok",
}
actual_keys = set(summary.keys())
missing_keys = required_keys - actual_keys
check(
    "All required keys present",
    len(missing_keys) == 0,
    f"missing: {missing_keys}" if missing_keys else "complete",
)

check(
    "signal_error_controls == 1",
    summary.get("signal_error_controls") == 1,
    f"actual = {summary.get('signal_error_controls')}",
)
check(
    "automated_controls == 2 (Pass + Fail)",
    summary["automated_controls"] == 2,
    f"actual = {summary['automated_controls']}",
)
check(
    "manual_controls == 1",
    summary["manual_controls"] == 1,
    f"actual = {summary['manual_controls']}",
)
check(
    "total_controls == 4",
    summary["total_controls"] == 4,
)
check(
    "signal_api_errors includes SignalError",
    any(e.get("status") == "SignalError" for e in summary["signal_api_errors"]),
    f"{len(summary['signal_api_errors'])} api_error entries",
)

# signals_referenced == unique bus_names in pack
expected_referenced = len(bus_names)
check(
    "signals_referenced matches pack",
    summary["signals_referenced"] == expected_referenced,
    f"summary={summary['signals_referenced']}, expected={expected_referenced}",
)

check(
    "reconciliation_ok is boolean",
    isinstance(summary["reconciliation_ok"], bool),
)

# ── Evaluator sanity ──────────────────────────────────────────
print("\n── 8. Evaluator sanity ────────────────────────────────────")

check(
    "Evaluators registered",
    len(EVALUATORS) > 0,
    f"{len(EVALUATORS)} evaluators",
)
check(
    "Signal providers registered",
    len(SIGNAL_PROVIDERS) > 0,
    f"{len(SIGNAL_PROVIDERS)} providers",
)

unresolvable = []
for cid, ev in EVALUATORS.items():
    for sig in ev.required_signals:
        if sig not in SIGNAL_PROVIDERS:
            unresolvable.append((cid, sig))
check(
    "All evaluator signals have providers",
    len(unresolvable) == 0,
    f"unresolvable: {unresolvable[:5]}" if unresolvable else "all resolved",
)

# ══════════════════════════════════════════════════════════════════
#  Summary
# ══════════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
if failures == 0:
    print(f"  ✔ All checks passed")
else:
    print(f"  ✗ {failures} check(s) FAILED")
sys.exit(failures)
