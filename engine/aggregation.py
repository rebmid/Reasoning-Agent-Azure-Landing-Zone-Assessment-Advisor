"""Enterprise-scale aggregation — multi-subscription scope model.

Enriches flat per-control results with enterprise metadata so downstream
consumers (CSA workbook, AI reasoning engine) see:

  - **Coverage metric** (e.g. 17/100 compliant)
  - **Affected subscription count**
  - **Scope level** (L1 Subscription / L2 Management Group / L3 Tenant)
  - **Scope pattern** (Platform Governance Gap / Isolated Drift / Moderate Spread)
  - **Representative evidence** (max 3 examples)

Design rules:
  - One result per control — never per-subscription rows.
  - Scoring math is **untouched**; these fields ride alongside existing ones.
  - No duplicate control entries.
"""
from __future__ import annotations

import re
from typing import Any


# ══════════════════════════════════════════════════════════════════
#  Scope Model  (L1 / L2 / L3)
# ══════════════════════════════════════════════════════════════════

def classify_scope_level(
    affected_pct: float,
    affected_count: int,
    total_subs: int,
) -> str:
    """Determine the scope level of a finding.

    Level 1 – Subscription:        Affects < 20 % **and** ≤ 2 subscriptions
    Level 2 – Management Group:    Affects 20–80 % of subscriptions
    Level 3 – Tenant-wide pattern: Affects > 80 % of subscriptions

    Single-subscription tenants always classify as L3.
    """
    if total_subs <= 1:
        return "Tenant"
    if affected_pct > 80:
        return "Tenant"
    if affected_pct >= 20 or affected_count > 2:
        return "Management Group"
    return "Subscription"


def classify_pattern(
    affected_pct: float,
    affected_count: int,
    total_subs: int,
) -> str:
    """Classify the remediation pattern for a finding.

    > 80 % lack → Platform Governance Gap  (systemic policy failure)
    20–80 %     → Moderate Spread          (MG-level inconsistency)
    < 20 %      → Isolated Drift           (edge-case outlier)
    """
    if total_subs <= 1:
        return "Platform Governance Gap" if affected_pct > 0 else "None"
    if affected_pct > 80:
        return "Platform Governance Gap"
    if affected_pct >= 20:
        return "Moderate Spread"
    if affected_count > 0:
        return "Isolated Drift"
    return "None"


# ══════════════════════════════════════════════════════════════════
#  Per-subscription extraction helpers
# ══════════════════════════════════════════════════════════════════

_SUB_ID_RX = re.compile(
    r"/subscriptions/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def _extract_subscription_ids_from_evidence(evidence: list[dict]) -> set[str]:
    """Pull unique subscription IDs from resource_id fields in evidence."""
    subs: set[str] = set()
    for ev in evidence:
        rid = ev.get("resource_id", "")
        m = _SUB_ID_RX.search(rid)
        if m:
            subs.add(m.group(1).lower())
        # Also check nested properties
        props = ev.get("properties", {})
        if isinstance(props, dict):
            for v in props.values():
                if isinstance(v, str):
                    m2 = _SUB_ID_RX.search(v)
                    if m2:
                        subs.add(m2.group(1).lower())
    return subs


def _build_coverage_display(
    coverage_ratio: float | None,
    applicable: int | None,
    compliant: int | None,
) -> str | None:
    """Format a human-readable coverage string like '17/100 compliant'."""
    if applicable is not None and compliant is not None and applicable > 0:
        return f"{compliant}/{applicable} compliant"
    if coverage_ratio is not None:
        pct = round(coverage_ratio * 100, 1)
        return f"{pct}% compliant"
    return None


# ══════════════════════════════════════════════════════════════════
#  Main enrichment entry point
# ══════════════════════════════════════════════════════════════════

def enrich_results_enterprise(
    results: list[dict[str, Any]],
    execution_context: dict[str, Any],
    signal_bus: Any | None = None,
    scope: Any | None = None,
) -> list[dict[str, Any]]:
    """Enrich each control result with enterprise-scale aggregation metadata.

    Adds the following fields to each result dict (in-place):

    - ``subscriptions_assessed``  — total subscriptions in assessment scope
    - ``subscriptions_affected``  — subscriptions where control status ≠ Pass
    - ``coverage_pct``            — compliant / applicable × 100 (or None)
    - ``coverage_display``        — human-readable string e.g. "17/100 compliant"
    - ``scope_level``             — "Subscription" | "Management Group" | "Tenant"
    - ``scope_pattern``           — "Platform Governance Gap" | "Isolated Drift" |
                                    "Moderate Spread" | "None"
    - ``sample_evidence``         — max 3 representative evidence items

    Does NOT change scoring fields (status, severity, confidence, etc.).
    Does NOT create new rows.
    """
    total_subs = execution_context.get("subscription_count_visible", 1) or 1
    scope_sub_ids = set(
        s.lower()
        for s in execution_context.get("subscription_ids_visible", [])
    )

    for r in results:
        status = r.get("status", "Manual")
        evidence = r.get("evidence", [])
        coverage_ratio = r.get("coverage_ratio")

        # ── Subscriptions assessed ────────────────────────────────
        r["subscriptions_assessed"] = total_subs

        # ── Subscriptions affected ────────────────────────────────
        # Try 1: evidence resource IDs (most accurate)
        ev_subs = _extract_subscription_ids_from_evidence(evidence)
        affected = len(ev_subs & scope_sub_ids) if ev_subs else 0

        # If we found sub IDs in evidence and control is failing,
        # those are the affected subs.  For passing controls, affected = 0.
        if status in ("Fail", "Partial"):
            if affected == 0:
                # Fallback: if we can't determine per-sub, assume all are
                # affected proportionally to coverage ratio.
                if coverage_ratio is not None and coverage_ratio < 1.0:
                    affected = max(1, round(total_subs * (1 - coverage_ratio)))
                else:
                    # Conservative: assume all subs affected when we can't tell
                    affected = total_subs
        elif status == "Pass":
            affected = 0
        elif status == "Manual":
            affected = 0  # No automated evidence

        r["subscriptions_affected"] = affected

        # ── Coverage ──────────────────────────────────────────────
        # Extract applicable/compliant from coverage data if available
        applicable = None
        compliant = None
        # Evaluators that return CoveragePayload have it in the
        # serialised result via the adapter
        if coverage_ratio is not None:
            # If evidence items have resource-level counts, use them
            r["coverage_pct"] = round(coverage_ratio * 100, 1)
            # Try to extract counts from the notes/evidence
            # (the adapter stores reason in 'notes')
            notes = r.get("notes", "")
            # Many evaluators embed "X/Y" patterns in their reason
            count_match = re.search(r"(\d+)\s*/\s*(\d+)", notes)
            if count_match:
                compliant = int(count_match.group(1))
                applicable = int(count_match.group(2))
        elif status in ("Fail", "Partial"):
            # Estimate from evidence count and subscription scope
            r["coverage_pct"] = 0.0
        elif status == "Pass":
            r["coverage_pct"] = 100.0
        else:
            r["coverage_pct"] = None

        r["coverage_display"] = _build_coverage_display(
            coverage_ratio, applicable, compliant,
        )

        # ── Scope level & pattern ─────────────────────────────────
        affected_pct = (affected / total_subs * 100) if total_subs else 0
        r["scope_level"] = classify_scope_level(
            affected_pct, affected, total_subs,
        )
        r["scope_pattern"] = classify_pattern(
            affected_pct, affected, total_subs,
        )

        # ── Representative evidence sample (max 3) ──────────────
        sample: list[dict] = []
        for ev in evidence[:3]:
            if isinstance(ev, dict):
                sample.append({
                    k: ev[k]
                    for k in ("resource_id", "summary", "type")
                    if k in ev
                })
        r["sample_evidence"] = sample

    return results


# ══════════════════════════════════════════════════════════════════
#  Aggregate summary for AI consumption
# ══════════════════════════════════════════════════════════════════

def build_enterprise_control_summary(
    results: list[dict[str, Any]],
    *,
    max_controls: int = 50,
) -> list[dict[str, Any]]:
    """Build the summarised, token-stable control payload for the AI engine.

    Returns a list of compact dicts — one per failing/partial control —
    sorted by risk score (descending).  Never includes raw per-subscription
    signal arrays.

    Shape per control::

        {
            "control_id": "NSG-001",
            "section": "Networking",
            "status": "Fail",
            "severity": "High",
            "coverage_percent": 12,
            "subscriptions_affected": 88,
            "subscriptions_assessed": 100,
            "risk_level": "High",
            "scope_level": "Tenant",
            "scope_pattern": "Platform Governance Gap",
            "coverage_display": "12/100 compliant",
            "sample_evidence": ["..."],
        }
    """
    controls: list[dict] = []
    for r in results:
        if r.get("status") not in ("Fail", "Partial"):
            continue

        # Derive risk_level from severity (simple mapping)
        sev = r.get("severity", "Medium")
        risk_map = {
            "Critical": "Critical",
            "High": "High",
            "Medium": "Medium",
            "Low": "Low",
            "Info": "Info",
        }
        risk_level = risk_map.get(sev, "Medium")

        # Elevate risk_level if scope is tenant-wide
        if r.get("scope_pattern") == "Platform Governance Gap" and risk_level == "Medium":
            risk_level = "High"

        controls.append({
            "control_id": r.get("control_id", ""),
            "section": r.get("section", ""),
            "status": r.get("status", ""),
            "severity": sev,
            "coverage_percent": r.get("coverage_pct"),
            "subscriptions_affected": r.get("subscriptions_affected", 0),
            "subscriptions_assessed": r.get("subscriptions_assessed", 0),
            "risk_level": risk_level,
            "scope_level": r.get("scope_level", ""),
            "scope_pattern": r.get("scope_pattern", ""),
            "coverage_display": r.get("coverage_display"),
            "sample_evidence": [
                ev.get("summary", str(ev))
                for ev in r.get("sample_evidence", [])[:3]
            ],
        })

    # Sort by risk level priority, then by subscriptions_affected descending
    _RISK_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}
    controls.sort(key=lambda c: (
        _RISK_ORDER.get(c["risk_level"], 5),
        -(c.get("subscriptions_affected") or 0),
    ))

    return controls[:max_controls]


def build_scope_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a high-level scope model summary for AI context.

    Returns::

        {
            "total_findings": 42,
            "by_scope_level": {
                "Subscription": {"count": 3, "pattern": "Isolated Drift"},
                "Management Group": {"count": 12, "pattern": "Moderate Spread"},
                "Tenant": {"count": 27, "pattern": "Platform Governance Gap"},
            },
            "governance_gap_percent": 64.3,
            "strategic_insight": "..."
        }
    """
    failing = [r for r in results if r.get("status") in ("Fail", "Partial")]
    total = len(failing)
    if total == 0:
        return {
            "total_findings": 0,
            "by_scope_level": {},
            "governance_gap_percent": 0.0,
            "strategic_insight": "No failing controls detected.",
        }

    by_level: dict[str, int] = {"Subscription": 0, "Management Group": 0, "Tenant": 0}
    by_pattern: dict[str, int] = {}
    for r in failing:
        level = r.get("scope_level", "Subscription")
        pattern = r.get("scope_pattern", "None")
        by_level[level] = by_level.get(level, 0) + 1
        by_pattern[pattern] = by_pattern.get(pattern, 0) + 1

    gov_gap = by_pattern.get("Platform Governance Gap", 0)
    gov_pct = round(gov_gap / total * 100, 1) if total else 0.0

    # Strategic insight
    if gov_pct > 60:
        insight = (
            f"{gov_pct}% of findings are tenant-wide Platform Governance Gaps. "
            "Remediation should target platform-level policy and governance "
            "controls before addressing subscription-specific drift."
        )
    elif gov_pct > 30:
        insight = (
            f"{gov_pct}% of findings are Platform Governance Gaps alongside "
            "Management-Group and Subscription-level issues. A layered "
            "remediation strategy addressing both platform policy and "
            "workload-level drift is recommended."
        )
    else:
        insight = (
            "Most findings are localised to specific subscriptions or "
            "management groups. Targeted remediation per workload scope "
            "will be more effective than blanket policy changes."
        )

    return {
        "total_findings": total,
        "by_scope_level": {
            level: {
                "count": count,
                "findings": [
                    r["control_id"][:8]
                    for r in failing
                    if r.get("scope_level") == level
                ][:5],
            }
            for level, count in by_level.items()
            if count > 0
        },
        "by_pattern": {k: v for k, v in by_pattern.items() if v > 0},
        "governance_gap_percent": gov_pct,
        "strategic_insight": insight,
    }
