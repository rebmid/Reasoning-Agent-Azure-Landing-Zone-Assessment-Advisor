"""Relationship Integrity Validator — compiler-grade structural checks.

Enforces deterministic relationships across the entire pipeline output
using **checklist_id** as the canonical identifier (no synthetic INIT-xxx):

  - Blocker → remediation item referential integrity
  - Remediation item → control mapping completeness
  - Roadmap → remediation item existence
  - Maturity trajectory formula integrity
  - Every checklist_id must be a valid ALZ review-checklist ID

If any check fails, rendering and downstream processing MUST halt.

Usage::

    from engine.relationship_integrity import validate_relationship_integrity

    ok, violations = validate_relationship_integrity(output)
    if not ok:
        for v in violations:
            print(v)
"""
from __future__ import annotations

import re
from typing import Any

from engine.id_rewriter import is_synthetic_id


# Valid checklist_id pattern: letter(s) + digits + dot + digits (e.g. A01.01, B02.03)
_CHECKLIST_ID_RE = re.compile(r"^[A-Z]\d{2}\.\d{2}$")


class IntegrityError(Exception):
    """Raised when relationship integrity validation fails."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__(
            f"Relationship integrity failed: {len(violations)} violation(s)"
        )


# ── Core validator ────────────────────────────────────────────────

def validate_relationship_integrity(output: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate all cross-references in the pipeline output.

    Prints diagnostic tables and returns (ok, violations).

    Parameters
    ----------
    output : dict
        Full pipeline output (either the top-level run dict or just
        the ``ai`` sub-dict).  The function auto-detects whether
        ``ai`` is nested or flat.

    Returns
    -------
    (ok, violations) : tuple[bool, list[str]]
        ``ok`` is True when zero violations are found.
    """
    # Normalise: accept both run-level dict and ai-level dict
    ai = output.get("ai", output)

    violations: list[str] = []

    esr = ai.get("enterprise_scale_readiness") or {}
    blockers = esr.get("blockers", [])

    # Remediation items — keyed by checklist_id
    remediation_items = ai.get("remediation_items", ai.get("initiatives", []))
    item_by_id: dict[str, dict] = {
        i["checklist_id"]: i
        for i in remediation_items
        if "checklist_id" in i
    }

    roadmap_src = ai.get("transformation_roadmap") or {}
    roadmap_phases = roadmap_src.get("roadmap_30_60_90") or {}
    results = output.get("results", [])
    results_by_id = {r["control_id"]: r for r in results if "control_id" in r}
    trajectory = ai.get("deterministic_trajectory") or {}

    # ── Table 1: Blocker → Remediation Item integrity ─────────────
    print("\n  ── Relationship Integrity: Blocker → Remediation Item ──")
    print(f"  {'Category':<30} {'resolving_checklist_ids':<30} {'all_exist':<10} {'all_valid':<10}")
    print(f"  {'─'*30} {'─'*30} {'─'*10} {'─'*10}")

    for b in blockers:
        category = b.get("category", "?")
        refs = b.get("resolving_checklist_ids", None)
        # Legacy fallback
        if refs is None:
            legacy = b.get("resolving_item", b.get("resolving_initiative"))
            refs = [legacy] if legacy else []

        all_exist = all(r in item_by_id for r in refs) if refs else False
        all_valid = all(_is_valid_checklist_id(r) for r in refs) if refs else False

        flag = "✓" if all_exist else "✗"
        id_flag = "✓" if all_valid else "✗"
        refs_str = ", ".join(refs) if refs else "(none)"
        print(f"  {category:<30} {refs_str:<30} {flag:<10} {id_flag:<10}")

        if not refs:
            # Warning only — AI may not produce remediation items for every
            # blocker category.  The blocker still renders; it just won't
            # have resolving items linked.
            print(f"  ⚠ BLOCKER_NULL_REF: blocker '{category}' has no "
                  f"resolving_checklist_ids (warning, not a violation).")
        else:
            for ref in refs:
                if ref not in item_by_id:
                    violations.append(
                        f"BLOCKER_REF: blocker '{category}' references "
                        f"'{ref}' which does not exist in remediation_items[]."
                    )
                if not _is_valid_checklist_id(ref):
                    violations.append(
                        f"BLOCKER_INVALID_ID: blocker '{category}' references "
                        f"'{ref}' which is not a valid checklist_id format."
                    )
                if is_synthetic_id(ref):
                    violations.append(
                        f"SYNTHETIC_ID: blocker '{category}' uses "
                        f"synthetic ID '{ref}' in resolving_checklist_ids."
                    )

    # ── Table 2: Remediation Item → Control mappings ──────────────
    print("\n  ── Relationship Integrity: Remediation Item → Controls ──")
    print(f"  {'checklist_id':<16} {'controls':<10} {'failing':<10} {'valid_id':<10}")
    print(f"  {'─'*16} {'─'*10} {'─'*10} {'─'*10}")

    for item in remediation_items:
        cid = item.get("checklist_id", "?")
        controls = item.get("controls", [])
        failing = sum(
            1 for c in controls
            if results_by_id.get(c, {}).get("status") in ("Fail", "Partial")
        )
        valid_id = _is_valid_checklist_id(cid)

        id_flag = "✓" if valid_id else "✗"
        print(f"  {cid:<16} {len(controls):<10} {failing:<10} {id_flag:<10}")

        if not valid_id:
            violations.append(
                f"ITEM_INVALID_ID: remediation item '{cid}' does not "
                f"match checklist_id format (e.g. A01.01)."
            )
        if is_synthetic_id(cid):
            violations.append(
                f"SYNTHETIC_ITEM_ID: remediation item '{cid}' uses a "
                f"synthetic ID — only canonical checklist_ids allowed."
            )
        if not controls:
            violations.append(
                f"ITEM_NO_CONTROLS: remediation item '{cid}' has no controls[]."
            )

    # ── Table 3: Roadmap → Remediation Item references ────────────
    print("\n  ── Relationship Integrity: Roadmap → Remediation Items ──")
    print(f"  {'phase':<12} {'checklist_id':<16} {'exists':<8}")
    print(f"  {'─'*12} {'─'*16} {'─'*8}")

    for phase_key in ("30_days", "60_days", "90_days"):
        entries = roadmap_phases.get(phase_key, [])
        for entry in entries:
            eid = entry.get("checklist_id", entry.get("initiative_id", ""))
            exists = eid in item_by_id

            flag = "✓" if exists else "✗"
            print(f"  {phase_key:<12} {eid:<16} {flag:<8}")

            if not eid:
                violations.append(
                    f"ROADMAP_NO_ID: roadmap entry in {phase_key} "
                    f"has no checklist_id."
                )
            elif not exists:
                violations.append(
                    f"ROADMAP_REF: roadmap entry '{eid}' in {phase_key} "
                    f"does not exist in remediation_items[]."
                )

    # ── Maturity trajectory formula check ─────────────────────────
    if trajectory:
        controls_resolved = trajectory.get("controls_resolved_by_phase", {})

        for phase_key in ("30_days", "60_days", "90_days"):
            resolved = controls_resolved.get(phase_key, 0)
            if resolved == 0:
                _check_trajectory_unchanged(
                    trajectory, phase_key, violations
                )

    # ── Summary ───────────────────────────────────────────────────
    ok = len(violations) == 0
    status = "✓ PASS" if ok else f"✗ FAIL ({len(violations)} violations)"
    print(f"\n  ── Relationship Integrity Result: {status} ──")
    if not ok:
        for v in violations:
            print(f"    • {v}")

    return ok, violations


def _check_trajectory_unchanged(
    trajectory: dict, phase_key: str, violations: list[str]
) -> None:
    """Verify trajectory stays flat when no controls are resolved in a phase."""
    phase_map = {
        "30_days": ("current_percent", "post_30_day_percent"),
        "60_days": ("post_30_day_percent", "post_60_day_percent"),
        "90_days": ("post_60_day_percent", "post_90_day_percent"),
    }
    prev_key, curr_key = phase_map.get(phase_key, ("", ""))
    if not prev_key:
        return

    prev_val = trajectory.get(prev_key)
    curr_val = trajectory.get(curr_key)
    if prev_val is not None and curr_val is not None:
        if abs(float(curr_val) - float(prev_val)) > 0.01:
            violations.append(
                f"TRAJECTORY_DRIFT: {curr_key}={curr_val} differs from "
                f"{prev_key}={prev_val} but controls_resolved_by_phase"
                f".{phase_key}=0. Trajectory MUST remain unchanged."
            )


def _is_valid_checklist_id(ref: str) -> bool:
    """Check if a string matches the ALZ review-checklist ID format.

    Valid examples: A01.01, B02.03, C01.12
    """
    if not ref:
        return False
    return bool(_CHECKLIST_ID_RE.match(ref))


# ── Convenience: raise on failure ─────────────────────────────────

def require_relationship_integrity(output: dict[str, Any]) -> list[str]:
    """Validate and raise ``IntegrityError`` if any violations exist.

    Returns the (empty) violation list on success.
    """
    ok, violations = validate_relationship_integrity(output)
    if not ok:
        raise IntegrityError(violations)
    return violations
