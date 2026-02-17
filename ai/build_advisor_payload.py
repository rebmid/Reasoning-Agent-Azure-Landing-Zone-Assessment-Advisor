"""Build the deterministic advisor payload — all fields the AI layer consumes.

New fields added for architectural decision support:
  - failing_controls        — full fail list with section + severity for clustering
  - initiative_candidates   — pre-clustered groups of related failing controls
  - dependency_order        — topologically sorted control IDs from the knowledge graph
  - design_area_maturity    — per-ALZ-design-area maturity derived from section_scores
  - platform_scale_limits   — tenant / subscription / RBAC scope context
  - signal_confidence       — aggregated signal availability for transparency

Enterprise-scale fields (require aggregation enrichment):
  - enterprise_controls     — summarised, token-stable per-control payloads
  - scope_summary           — L1/L2/L3 breakdown with strategic insight
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


# ── ALZ design-area ↔ section mapping ─────────────────────────────
# Maps assessment section names to official ALZ design areas so the AI
# can reason in platform terms.
_SECTION_TO_DESIGN_AREA: dict[str, str] = {
    "Security": "Security",
    "Networking": "Network Topology and Connectivity",
    "Network Coverage": "Network Topology and Connectivity",
    "Governance": "Governance",
    "Identity": "Identity and Access Management",
    "Management": "Management",
    "Data Protection": "Security",
    "Resilience": "Management",
    "Platform": "Platform Automation and DevOps",
    "Cost": "Governance",
}


def _build_design_area_maturity(section_scores: list[dict]) -> list[dict]:
    """Aggregate section_scores into per-ALZ-design-area maturity."""
    area_data: dict[str, dict] = defaultdict(lambda: {
        "total_auto": 0, "total_pass": 0, "gaps": [],
    })
    for ss in section_scores:
        area = _SECTION_TO_DESIGN_AREA.get(ss["section"], ss["section"])
        area_data[area]["total_auto"] += ss.get("automated_controls", 0)
        area_data[area]["total_pass"] += ss.get("automated_pass", 0)
        # Collect top gaps from lowest-maturity sections
        if ss.get("maturity_percent") is not None and ss["maturity_percent"] < 60:
            area_data[area]["gaps"].append(ss["section"])

    out = []
    for area, d in sorted(area_data.items()):
        mat = round((d["total_pass"] / d["total_auto"]) * 100.0, 1) if d["total_auto"] else 0.0
        rate = round(d["total_pass"] / d["total_auto"], 3) if d["total_auto"] else 0.0
        out.append({
            "design_area": area,
            "maturity_percent": mat,
            "control_pass_rate": rate,
            "top_gaps": d["gaps"][:5],
        })
    out.sort(key=lambda x: x["maturity_percent"])
    return out


def _build_platform_scale_limits(execution_context: dict) -> dict:
    """Derive platform scale context from execution_context."""
    return {
        "subscription_count": execution_context.get("subscription_count_visible", 0),
        "management_group_depth": execution_context.get("management_group_depth", 0),
        "identity_type": execution_context.get("identity_type", "unknown"),
        "rbac_highest_role": execution_context.get("rbac_highest_role", "unknown"),
        "multi_tenant": execution_context.get("multi_tenant", False),
    }


def _cluster_initiative_candidates(fails: list[dict]) -> list[dict]:
    """Pre-cluster failing controls by section into initiative candidates.

    This gives the LLM a deterministic starting point for initiative
    creation — it can merge, split, or re-scope, but the clusters
    reduce hallucination risk.
    """
    by_section: dict[str, list[str]] = defaultdict(list)
    for f in fails:
        section = f.get("section") or f.get("domain") or "Unknown"
        by_section[section].append(f["control_id"])

    return [
        {
            "design_area": _SECTION_TO_DESIGN_AREA.get(section, section),
            "section": section,
            "control_ids": cids,
            "control_count": len(cids),
        }
        for section, cids in sorted(by_section.items())
    ]


def _build_enterprise_controls(results: list[dict]) -> list[dict]:
    """Build summarised, token-stable per-control payloads for the AI.

    Each failing/partial control becomes a compact dict with
    coverage %, subscriptions affected, risk level, and scope classification.
    Never includes raw per-subscription signal arrays — keeps token size
    stable regardless of subscription count.

    Uses enterprise enrichment fields if present (from
    ``engine.aggregation.enrich_results_enterprise``), otherwise falls
    back to basic metadata.
    """
    _RISK_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}
    controls: list[dict] = []
    for r in results:
        if r.get("status") not in ("Fail", "Partial"):
            continue

        sev = r.get("severity", "Medium")
        risk_level = sev
        # Elevate risk if scope is tenant-wide governance gap
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
        })

    controls.sort(key=lambda c: (
        _RISK_ORDER.get(c["risk_level"], 5),
        -(c.get("subscriptions_affected") or 0),
    ))
    return controls[:50]


def _build_scope_summary(results: list[dict]) -> dict:
    """Build high-level L1/L2/L3 scope breakdown for AI context."""
    failing = [r for r in results if r.get("status") in ("Fail", "Partial")]
    total = len(failing)
    if total == 0:
        return {"total_findings": 0, "strategic_insight": "No failing controls."}

    by_level: dict[str, int] = {}
    by_pattern: dict[str, int] = {}
    for r in failing:
        level = r.get("scope_level", "Unknown")
        pattern = r.get("scope_pattern", "Unknown")
        by_level[level] = by_level.get(level, 0) + 1
        by_pattern[pattern] = by_pattern.get(pattern, 0) + 1

    gov_gap = by_pattern.get("Platform Governance Gap", 0)
    gov_pct = round(gov_gap / total * 100, 1) if total else 0.0

    return {
        "total_findings": total,
        "by_scope_level": by_level,
        "by_pattern": by_pattern,
        "governance_gap_percent": gov_pct,
    }


def build_advisor_payload(
    scoring: dict,
    results: list[dict],
    execution_context: dict,
    delta: dict | None = None,
    mg_hierarchy: dict | None = None,
    dependency_order: list[str] | None = None,
    signal_availability: dict | None = None,
) -> dict[str, Any]:
    """
    Build a compact, token-safe payload for AI clustering / advisory.
    Only the fields the model needs — never the full raw result set.

    New parameters (all optional, backwards-compatible):
      dependency_order   — topologically sorted control IDs from knowledge graph
      signal_availability — output of probe_signal_availability()
    """

    fails = [
        {
            "control_id": r["control_id"],
            "description": r.get("description"),
            "severity": r.get("severity"),
            "domain": r.get("domain"),
            "section": r.get("section"),
        }
        for r in results
        if r["status"] == "Fail"
    ]

    manual = [
        {
            "control_id": r["control_id"],
            "description": r.get("description"),
            "severity": r.get("severity"),
            "domain": r.get("domain"),
        }
        for r in results
        if r["status"] == "Manual"
        and r.get("severity") in ("High", "Critical")
    ][:25]

    section_scores = scoring.get("section_scores", [])

    # ── Signal confidence ─────────────────────────────────────────
    # signal_availability is {source: [{signal, status, icon, ms}]}
    sig_conf: dict[str, Any] = {}
    if signal_availability:
        all_entries = [
            entry
            for entries in signal_availability.values()
            for entry in entries
        ]
        total = len(all_entries)
        available = sum(1 for e in all_entries if e.get("status") == "OK")
        low_conf = [
            e.get("signal", "unknown")
            for e in all_entries
            if e.get("status") != "OK"
        ]
        sig_conf = {
            "total_signals_probed": total,
            "signals_available": available,
            "signals_unavailable": total - available,
            "availability_percent": round((available / total) * 100, 1) if total else 0.0,
            "low_confidence_areas": low_conf[:10],
        }

    return {
        "execution_context": execution_context,
        "overall_maturity": scoring["overall_maturity_percent"],
        "section_scores": section_scores,
        "top_failing_sections": scoring.get("top_failing_sections", [])[:5],
        "most_impactful_gaps": scoring.get("most_impactful_gaps", [])[:15],
        "failed_controls": fails,
        "sampled_manual_controls": manual,
        "delta": delta,
        "management_group_hierarchy": mg_hierarchy,
        # ── New architectural decision support fields ─────────────
        "failing_controls": fails,
        "initiative_candidates": _cluster_initiative_candidates(fails),
        "dependency_order": dependency_order or [],
        "design_area_maturity": _build_design_area_maturity(section_scores),
        "platform_scale_limits": _build_platform_scale_limits(
            execution_context or {}
        ),
        "signal_confidence": sig_conf,
        # ── Enterprise-scale aggregation (token-stable) ───────────
        "enterprise_controls": _build_enterprise_controls(results),
        "scope_summary": _build_scope_summary(results),
    }
