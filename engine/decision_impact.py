"""Decision Impact Model — per-item "what breaks if not implemented".

Computes enterprise_scale_blocked, critical_risks_remaining,
fail_controls_remaining, blocked_items, and maturity_ceiling
from deterministic joins of remediation items, results, risks, and blockers.

Layer: Derived models (deterministic joins only — no creative logic).
"""
from __future__ import annotations

import json
from pathlib import Path

from engine.guardrails import (
    empty_evidence_refs,
    compute_derived_confidence,
    insufficient_evidence_marker,
)

from schemas.taxonomy import BLOCKER_CATEGORY_TO_SECTIONS

# ── Control-pack section metadata (cached) ───────────────────────
_CONTROLS_JSON_PATH = (
    Path(__file__).resolve().parent.parent
    / "control_packs" / "alz" / "v1.0" / "controls.json"
)
_PACK_SECTION_MAP: dict[str, str] | None = None


def _load_pack_sections() -> dict[str, str]:
    """Return {8-char-key → lowercase design_area} from the control pack."""
    global _PACK_SECTION_MAP
    if _PACK_SECTION_MAP is None:
        with open(_CONTROLS_JSON_PATH, encoding="utf-8") as f:
            pack = json.load(f)
        _PACK_SECTION_MAP = {
            k: v.get("design_area", "") for k, v in pack.get("controls", {}).items()
            if isinstance(v, dict)
        }
    return _PACK_SECTION_MAP


# Direct mapping: blocker category (lowercase) → set of pack design_area values.
# The pack uses short names (network, cost, data_protection) while the taxonomy
# uses full titles (Network Topology and Connectivity, Networking).  This bridge
# lets Strategy 2b match without going through the mismatched section names.
_BLOCKER_TO_PACK_AREAS: dict[str, set[str]] = {
    "network topology and connectivity": {"network"},
    "networking":                         {"network"},
    "identity and access management":     {"identity"},
    "identity":                           {"identity"},
    "security":                           {"security", "data_protection"},
    "management":                         {"management"},
    "governance":                         {"governance"},
    "resource organization":              {"governance"},
    "data protection":                    {"data_protection", "security"},
    "resilience":                         {"resilience"},
    "cost governance":                    {"cost", "governance"},
    "automation":                         {"platform_automation"},
    "platform automation and devops":     {"platform_automation"},
    "billing":                            {"billing"},
    "azure billing and microsoft entra id tenants": {"billing"},
}


def _build_item_index(items: list[dict]) -> dict:
    """Map checklist_id → remediation item dict."""
    return {
        item.get("checklist_id", ""): item
        for item in items
        if item.get("checklist_id")
    }


def _build_dependency_reverse_map(items: list[dict]) -> dict[str, list[str]]:
    """Map checklist_id → list of checklist_ids that depend on it."""
    reverse: dict[str, list[str]] = {}
    for item in items:
        cid = item.get("checklist_id", "")
        for dep in item.get("dependencies", []):
            reverse.setdefault(dep, []).append(cid)
    return reverse


def _controls_for_item(item: dict) -> set[str]:
    """Extract control_id set from a remediation item."""
    return set(item.get("controls", []))


# Use canonical mapping from schemas.taxonomy
_BLOCKER_CATEGORY_TO_SECTIONS = BLOCKER_CATEGORY_TO_SECTIONS


def resolve_blockers_to_items(
    blockers: list[dict],
    items: list[dict],
    results: list[dict],
) -> dict[str, list[str]]:
    """
    Deterministically resolve blockers to remediation items via control overlap.

    For each blocker:
      1. If the blocker has affected_controls, find all items whose
         controls overlap with those affected controls, ranked by overlap.
      2. If no affected_controls, match by category → section → items
         with the most failing controls in that section.
      3. Falls back to existing resolving_checklist_ids / resolving_item
         only if no deterministic match is found AND the references are valid.

    Returns: dict of blocker_key → list of checklist_ids (empty list if unmappable)
    """
    results_by_id = {r.get("control_id", ""): r for r in results if r.get("control_id")}

    # Build item → controls set index
    item_controls_map: dict[str, set[str]] = {}
    for item in items:
        cid = item.get("checklist_id", "")
        if cid:
            item_controls_map[cid] = set(item.get("controls", []))

    # Build item → sections covered
    item_sections: dict[str, set[str]] = {}
    for item in items:
        cid = item.get("checklist_id", "")
        if not cid:
            continue
        sections = set()
        for ctrl_id in item.get("controls", []):
            ctrl = results_by_id.get(ctrl_id, {})
            sec = ctrl.get("section", "")
            if sec:
                sections.add(sec)
        item_sections[cid] = sections

    valid_item_ids = set(item_controls_map.keys())

    blocker_item_map: dict[str, list[str]] = {}

    for b in blockers:
        raw_key = b.get("category", "") or b.get("description", "")
        if not raw_key:
            continue
        # Normalize key to lowercase — patch_blocker_items also
        # lowercases the category at lookup time.
        blocker_key = raw_key.lower()

        # Strategy 1: Match by affected_controls overlap
        affected = set(b.get("affected_controls", []))
        if affected:
            # Collect all items with non-zero overlap, sorted by overlap desc
            scored = []
            for cid, ctrl_set in item_controls_map.items():
                overlap = len(affected & ctrl_set)
                if overlap > 0:
                    scored.append((overlap, cid))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                blocker_item_map[blocker_key] = [cid for _, cid in scored]
                continue

        # Strategy 2: Match by category → section → items with
        # failing controls in that section
        category = blocker_key  # already lowercase
        target_sections = _BLOCKER_CATEGORY_TO_SECTIONS.get(category, [])
        if target_sections:
            scored = []
            for cid, ctrl_set in item_controls_map.items():
                # Count failing controls in target sections
                fail_count = 0
                for ctrl_id in ctrl_set:
                    ctrl = results_by_id.get(ctrl_id, {})
                    if (ctrl.get("section", "") in target_sections
                            and ctrl.get("status") in ("Fail", "Partial")):
                        fail_count += 1
                if fail_count > 0:
                    scored.append((fail_count, cid))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                blocker_item_map[blocker_key] = [cid for _, cid in scored]
                continue

            # Also try matching by item section coverage
            matched = []
            for cid, sections in item_sections.items():
                if any(ts in sections for ts in target_sections):
                    matched.append(cid)
            if matched:
                blocker_item_map[blocker_key] = matched
                continue

        # Strategy 2b: Match via control-pack design_area metadata.
        # Strategies 1–2 rely on results_by_id which keys by full
        # control_id from evaluator output.  Item controls[] use
        # 8-char pack keys that may not appear in results_by_id.
        # Fall through here to resolve via the pack's own design_area field,
        # using _BLOCKER_TO_PACK_AREAS to bridge the naming gap.
        target_pack_areas = _BLOCKER_TO_PACK_AREAS.get(blocker_key)
        if target_pack_areas:
            pack_sections = _load_pack_sections()
            matched_pack = []
            for cid, ctrl_set in item_controls_map.items():
                for ctrl_key in ctrl_set:
                    pack_area = pack_sections.get(ctrl_key, "")
                    if pack_area in target_pack_areas:
                        matched_pack.append(cid)
                        break
            if matched_pack:
                blocker_item_map[blocker_key] = matched_pack
                continue

        # Strategy 3: Fallback to existing resolving_checklist_ids / resolving_item
        # (only if the referenced items actually exist)
        llm_refs = b.get("resolving_checklist_ids", [])
        if not llm_refs:
            # Legacy fallback: singular resolving_item or resolving_initiative
            single_ref = b.get("resolving_item", b.get("resolving_initiative", ""))
            if single_ref:
                llm_refs = [single_ref]
        valid_refs = [r for r in llm_refs if r in valid_item_ids]
        if valid_refs:
            blocker_item_map[blocker_key] = valid_refs
            continue

        # Strategy 4: Match by checklist_id prefix letter.
        # The ALZ checklist uses letter prefixes (A-H) that map to
        # design areas.  If all prior strategies failed, find items
        # whose checklist_id prefix corresponds to the blocker's area.
        from schemas.taxonomy import DESIGN_AREA_TO_CHECKLIST_LETTER
        area_name = None
        for official, letter in DESIGN_AREA_TO_CHECKLIST_LETTER.items():
            if official.lower() == blocker_key or blocker_key in official.lower():
                area_name = official
                break
        if area_name:
            target_letter = DESIGN_AREA_TO_CHECKLIST_LETTER.get(area_name, "")
            if target_letter:
                prefix_matched = [
                    cid for cid in valid_item_ids
                    if cid.startswith(target_letter)
                ]
                if prefix_matched:
                    blocker_item_map[blocker_key] = prefix_matched
                    continue

        # No deterministic match — empty list
        blocker_item_map[blocker_key] = []

    return blocker_item_map


def _count_risks_for_controls(
    control_ids: set[str],
    top_risks: list[dict],
) -> int:
    """Count how many top business risks overlap with a set of controls."""
    count = 0
    for risk in top_risks:
        affected = set(risk.get("affected_controls", []))
        if affected & control_ids:
            count += 1
    return count


def _affected_risk_titles(
    control_ids: set[str],
    top_risks: list[dict],
) -> list[str]:
    """Return titles of risks that overlap with control_ids."""
    titles = []
    for risk in top_risks:
        affected = set(risk.get("affected_controls", []))
        if affected & control_ids:
            titles.append(risk.get("title", "unnamed risk"))
    return titles


def _maturity_ceiling_if_skipped(
    item: dict,
    results: list[dict],
    section_scores: list[dict],
) -> str:
    """
    Compute a human-readable maturity ceiling statement.

    If we skip this remediation item, the affected design area(s) cannot
    improve beyond their current maturity.
    """
    init_controls = set(item.get("controls", []))
    if not init_controls:
        return insufficient_evidence_marker()

    # Find which sections are affected
    affected_sections: dict[str, int] = {}
    for r in results:
        if r.get("control_id") in init_controls and r.get("status") == "Fail":
            section = r.get("section") or r.get("alz_design_area") or "Unknown"
            affected_sections[section] = affected_sections.get(section, 0) + 1

    if not affected_sections:
        return "No failing controls in this item — maturity ceiling not impacted."

    # Map to current maturity
    section_maturity = {
        s.get("section") or s.get("alz_design_area", ""): s.get("maturity_percent", 0)
        for s in section_scores
    }

    parts = []
    for section, fail_count in sorted(affected_sections.items(), key=lambda x: -x[1]):
        current = section_maturity.get(section, 0)
        parts.append(f"{section} capped at ~{current:.0f}% ({fail_count} failing controls unresolved)")

    return "; ".join(parts)


def build_decision_impact_model(
    initiatives: list[dict],
    results: list[dict],
    top_risks: list[dict],
    blockers: list[dict],
    section_scores: list[dict],
    signals: dict | None = None,
) -> dict:
    """
    Build the decision impact model: per-item "if not implemented" analysis.

    Every output item includes evidence_refs and assumptions. No freeform inference.

    Parameters
    ----------
    initiatives : list[dict]
        Remediation items (keyed by checklist_id).
    results : list[dict]
        Assessment control results.
    top_risks : list[dict]
        Top business risks from executive pass.
    blockers : list[dict]
        Enterprise readiness blockers.
    section_scores : list[dict]
        Per-section maturity scores.
    signals : dict | None
        Signal data for evidence linking.

    Returns
    -------
    dict conforming to decision_impact_model schema.
    """
    item_index = _build_item_index(initiatives)
    dep_reverse = _build_dependency_reverse_map(initiatives)

    # Build blocker → item mapping (DETERMINISTIC)
    # Derive the mapping from control overlap:
    # A blocker maps to an item whose controls include any of
    # the blocker's affected controls, or whose section/category matches.
    blocker_item_map = resolve_blockers_to_items(blockers, initiatives, results)

    items = []
    for init in initiatives:
        cid = init.get("checklist_id", "")
        init_controls = _controls_for_item(init)

        # Controls that remain failing
        fail_controls = [
            r.get("control_id", "")
            for r in results
            if r.get("control_id") in init_controls and r.get("status") == "Fail"
        ]
        fail_controls = [c for c in fail_controls if c]

        # Risks that remain
        risk_titles = _affected_risk_titles(init_controls, top_risks)

        # Blocked downstream items
        blocked = dep_reverse.get(cid, [])

        # Enterprise-scale blocked: if this item resolves a blocker
        resolves_blockers = [
            cat for cat, res_ids in blocker_item_map.items()
            if cid in res_ids
        ]
        enterprise_blocked = len(resolves_blockers) > 0

        # Maturity ceiling
        ceiling = _maturity_ceiling_if_skipped(init, results, section_scores)


        # Confidence: average of underlying control confidences.
        # Only include controls that actually have a confidence_score.
        # Do NOT substitute a default — missing means missing.
        ctrl_confidences = [
            r["confidence_score"]
            for r in results
            if r.get("control_id") in init_controls
            and "confidence_score" in r
            and isinstance(r["confidence_score"], (int, float))
        ]
        # Signal coverage for this initiative's signals
        init_signals = set()
        for r in results:
            if r.get("control_id") in init_controls:
                init_signals.update(r.get("signals_used", []))
        signals_dict = signals or {}
        covered = sum(1 for s in init_signals if signals_dict.get(s))
        signal_pct = (covered / max(len(init_signals), 1)) * 100

        confidence = compute_derived_confidence(list(map(float, ctrl_confidences)), signal_pct)

        di_item = {
            "checklist_id": cid,
            "if_not_implemented": {
                "enterprise_scale_blocked": enterprise_blocked,
                "critical_risks_remaining": len(risk_titles),
                "fail_controls_remaining": len(fail_controls),
                "blocked_items": blocked,
                "maturity_ceiling_notes": ceiling,
            },
            "confidence": confidence,
            "evidence_refs": {
                "controls": list(init_controls)[:15] if init_controls else fail_controls[:15],
                "risks": risk_titles[:5],
                "blockers": resolves_blockers[:5],
                "signals": [f"signal:{s}" for s in sorted(init_signals)[:10]],
                "mcp_queries": [],
            },
            "assumptions": [
                "Impact computed from current assessment results",
                "Assumes no partial implementation or alternative mitigations",
            ],
        }

        items.append(di_item)

    return {
        "items": items,
    }
