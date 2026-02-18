"""Deterministic platform risk scoring — no LLM involved.

Risk is derived entirely from control metadata:
  severity, status, scope, dependency fan-out (foundational weight).

Risk Formula
────────────
  risk_score = severity_weight × blast_radius × foundational_weight

  severity_weight : High=3, Medium=2, Low=1, Info=0
  blast_radius    : Tenant=3, ManagementGroup=2, Subscription=1
  foundational_weight : 2 if control is depended on by others, else 1

Tier Thresholds
────────────────
  Critical : risk_score ≥ 12  (e.g. High + Tenant + Foundational = 3×3×2 = 18)
  High     : risk_score ≥ 6
  Medium   : risk_score ≥ 3
  Hygiene  : risk_score < 3
"""
from __future__ import annotations

import json
import os
from typing import Any

# ── Weight tables ─────────────────────────────────────────────────

_SEVERITY_WEIGHT: dict[str, int] = {
    "High": 3,
    "Medium": 2,
    "Low": 1,
    "Info": 0,
}

_SCOPE_MULTIPLIER: dict[str, int] = {
    "Tenant": 3,
    "ManagementGroup": 2,
    "Subscription": 1,
}

_TIER_THRESHOLDS: list[tuple[str, int]] = [
    ("Critical", 12),
    ("High", 6),
    ("Medium", 3),
]


# ── Internal helpers ──────────────────────────────────────────────

def _load_foundational_ids() -> set[str]:
    """Return the set of short control IDs that are depended on by others."""
    controls_path = os.path.join(os.path.dirname(__file__), "..", "graph", "controls.json")
    controls_path = os.path.normpath(controls_path)
    try:
        with open(controls_path, encoding="utf-8") as f:
            data = json.load(f)
        controls = data.get("controls", {})
        parents: set[str] = set()
        for ctrl in controls.values():
            for dep in ctrl.get("depends_on", []):
                parents.add(dep)
        return parents
    except Exception:
        return set()


# Cached at module level (immutable graph data)
_FOUNDATIONAL: set[str] | None = None


def _get_foundational() -> set[str]:
    global _FOUNDATIONAL
    if _FOUNDATIONAL is None:
        _FOUNDATIONAL = _load_foundational_ids()
    return _FOUNDATIONAL


def _short_id(control_id: str) -> str:
    return control_id[:8] if len(control_id) > 8 else control_id


def _tier_for_score(score: int) -> str:
    for tier, threshold in _TIER_THRESHOLDS:
        if score >= threshold:
            return tier
    return "Hygiene"


# ── Public API ────────────────────────────────────────────────────

def score_control(result: dict) -> dict:
    """Score a single control result and return a risk dict.

    Parameters
    ----------
    result : dict
        A ScoringResult / control result from the assessment engine.

    Returns
    -------
    dict with keys: control_id, short_id, text, section, severity,
    status, scope_level, is_foundational, risk_score, risk_tier,
    notes, evidence_count, confidence.
    """
    cid = result.get("control_id", "")
    sid = _short_id(cid)
    severity = result.get("severity", "Medium")
    status = result.get("status", "Unknown")
    scope = result.get("scope_level", "Tenant") or "Tenant"
    is_foundational = sid in _get_foundational()

    sev_w = _SEVERITY_WEIGHT.get(severity, 1)
    scope_w = _SCOPE_MULTIPLIER.get(scope, 1)
    found_w = 2 if is_foundational else 1

    risk_score = sev_w * scope_w * found_w
    risk_tier = _tier_for_score(risk_score)

    return {
        "control_id": cid,
        "short_id": sid,
        "text": result.get("text", ""),
        "section": result.get("section", ""),
        "severity": severity,
        "status": status,
        "scope_level": scope,
        "is_foundational": is_foundational,
        "risk_score": risk_score,
        "risk_tier": risk_tier,
        "notes": result.get("notes", ""),
        "evidence_count": result.get("evidence_count", 0),
        "confidence": result.get("confidence", ""),
        "coverage_display": result.get("coverage_display", ""),
    }


# Statuses that represent an active risk (not Pass, not NA, not Manual)
_RISK_STATUSES = {"Fail", "Partial", "SignalError", "Error"}


def score_all(results: list[dict]) -> dict[str, list[dict]]:
    """Score all control results and bucket into risk tiers.

    Returns
    -------
    dict keyed by tier ("Critical", "High", "Medium", "Hygiene").
    Each value is a list of scored control dicts sorted by risk_score desc.
    Only Fail/Partial/SignalError controls are included.
    """
    tiers: dict[str, list[dict]] = {
        "Critical": [],
        "High": [],
        "Medium": [],
        "Hygiene": [],
    }

    for result in results:
        if result.get("status") not in _RISK_STATUSES:
            continue
        scored = score_control(result)
        tiers[scored["risk_tier"]].append(scored)

    # Sort each tier by risk_score descending, then by section
    for tier in tiers.values():
        tier.sort(key=lambda x: (-x["risk_score"], x["section"]))

    return tiers


def build_risk_overview(results: list[dict]) -> dict[str, Any]:
    """Build the full platform risk overview for reporting.

    Returns
    -------
    dict with:
      tiers : dict[str, list[dict]] — bucketed scored controls
      summary : dict — counts per tier + total
      formula : str — human-readable formula description
    """
    tiers = score_all(results)

    summary = {
        "critical_count": len(tiers["Critical"]),
        "high_count": len(tiers["High"]),
        "medium_count": len(tiers["Medium"]),
        "hygiene_count": len(tiers["Hygiene"]),
        "total_risk_count": sum(len(v) for v in tiers.values()),
    }

    return {
        "tiers": tiers,
        "summary": summary,
        "formula": (
            "risk_score = severity_weight × blast_radius × foundational_weight  "
            "(Severity: High=3, Med=2, Low=1 · Scope: Tenant=3, MG=2, Sub=1 · "
            "Foundational=×2 if depended-on)"
        ),
    }
