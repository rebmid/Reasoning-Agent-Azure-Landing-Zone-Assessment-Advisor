"""Pipeline Utilities — readiness normalization, blocker patching, integrity checks.

┌─────────────────────────────────────────────────────────────────┐
│                  LAYER 2 — CHECKLIST MAPPING                    │
│                                                                 │
│  Post-processing guards applied to AI output before it leaves   │
│  the pipeline.  Normalises IDs, clamps scores, validates        │
│  structural integrity.                                          │
│                                                                 │
│  Called FROM Layer 3 (reasoning engine) as a downward           │
│  dependency — never the reverse.                                │
│                                                                 │
│  This module is FROZEN during stabilization.                    │
│  Do NOT add AI imports, prompt strings, or model calls.         │
└─────────────────────────────────────────────────────────────────┘

Formerly the "Initiative ID Rewriter" — the synthetic INIT-xxx layer has been
removed.  Checklist IDs from the Azure review-checklists repository are now
the canonical identifiers.

Retained utilities:
  - ``normalize_control_ids``       — canonical control ID normalization at AI ingestion
  - ``clamp_readiness_score``       — clamp readiness score to valid range
  - ``patch_blocker_items``         — patch blockers with resolving checklist_id
  - ``validate_pipeline_integrity`` — structural integrity check suite
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Valid checklist_id pattern: letter(s) + digits + dot + digits (e.g. A01.01)
_CHECKLIST_ID_RE = re.compile(r"^[A-Z]\d{2}\.\d{2}$")

# Synthetic ID patterns that must NEVER appear in canonical fields.
# Catches: INIT-xxx, slug-001 style, UUIDs, and any non-checklist format.
_SYNTHETIC_ID_PATTERNS = [
    re.compile(r"^INIT-\d+$", re.IGNORECASE),                    # INIT-001, INIT-005
    re.compile(r"^[a-z]+-[a-z]+-\d+$", re.IGNORECASE),           # monitor-workspace-001
    re.compile(r"^[a-z]+-[a-z]+-[a-z]+-\d+$", re.IGNORECASE),    # cost-forecast-baseline-001
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.IGNORECASE),      # UUID-style
]


def is_synthetic_id(value: str) -> bool:
    """Return True if the value matches a known synthetic ID pattern."""
    return any(p.match(value) for p in _SYNTHETIC_ID_PATTERNS)


# ── Canonical Control ID Normalization ────────────────────────────
# The deterministic layer must never tolerate identifier drift.
# AI output may emit full-length control IDs (e.g. "cost-forecast-001",
# "e6c4cfd3-e504-4547-a244-7ec66138a720") but the canonical control
# pack uses 8-character truncated keys (e.g. "cost-for", "e6c4cfd3").
#
# Strategy:
#   1. Exact match     → keep as-is (already canonical)
#   2. Prefix match    → rewrite to canonical key (first 8 chars match
#                         exactly one canonical key)
#   3. Ambiguous/none  → reject with structural violation

_CONTROLS_JSON_PATH = (
    Path(__file__).resolve().parent.parent
    / "control_packs" / "alz" / "v1.0" / "controls.json"
)

_CANONICAL_KEYS: set[str] | None = None


def _load_canonical_keys() -> set[str]:
    """Load the set of canonical control keys from controls.json (cached)."""
    global _CANONICAL_KEYS
    if _CANONICAL_KEYS is None:
        with open(_CONTROLS_JSON_PATH, encoding="utf-8") as f:
            pack = json.load(f)
        _CANONICAL_KEYS = set(pack.get("controls", {}).keys())
    return _CANONICAL_KEYS


def _resolve_control_id(raw_id: str, canonical_keys: set[str]) -> tuple[str, str]:
    """Resolve a single raw control ID to its canonical form.

    Returns
    -------
    tuple[str, str]
        (resolved_id, status) where status is one of:
        - "exact"    — already canonical
        - "prefix"   — resolved via prefix match
        - "reject"   — no match or ambiguous match
    """
    # 1. Exact match
    if raw_id in canonical_keys:
        return raw_id, "exact"

    # 2. Prefix match — the canonical keys may be 8-char truncated GUIDs
    #    or longer names like "vnet-peering-001".
    #    Strategy A: raw_id[:8] matches a canonical key exactly.
    prefix = raw_id[:8]
    matches = [k for k in canonical_keys if k == prefix]
    if len(matches) == 1:
        return matches[0], "prefix"

    # 3. Substring prefix — a canonical key starts with the raw_id
    #    (e.g., raw_id="vnet-peer" → canonical "vnet-peering-001")
    starts_matches = [k for k in canonical_keys if k.startswith(raw_id)]
    if len(starts_matches) == 1:
        return starts_matches[0], "prefix"

    # 4. Reverse prefix — the raw_id starts with a canonical key
    #    (e.g., raw_id="defender-assessments-001-extra" → "defender-assessments-001")
    reverse_matches = [k for k in canonical_keys if raw_id.startswith(k)]
    if len(reverse_matches) == 1:
        return reverse_matches[0], "prefix"

    # 5. Fuzzy: canonical key starts with raw_id[:8]
    #    (e.g., raw_id="policy-e" → match "policy-exemptions-001")
    fuzzy_matches = [k for k in canonical_keys if k.startswith(prefix)]
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], "prefix"

    # 6. Name-based: match by common word fragments for short partial IDs
    #    (e.g., raw_id="ddos-prot" → match control with "ddos" in key)
    if len(raw_id) >= 4:
        word = raw_id.split("-")[0].lower()
        word_matches = [k for k in canonical_keys if word in k.lower()]
        if len(word_matches) == 1:
            return word_matches[0], "prefix"

    # 7. No match
    return raw_id, "reject"


def normalize_control_ids(
    items: list[dict],
    canonical_keys: set[str] | None = None,
) -> list[str]:
    """Normalize control IDs in AI-generated items to canonical 8-char keys.

    Modifies items in-place.  Each item's ``controls`` list is rewritten
    so that every entry is a canonical key from ``controls.json``.

    Rejected IDs are removed from the controls list and recorded as
    pipeline violations.

    Parameters
    ----------
    items : list[dict]
        Remediation items from the AI roadmap pass, each with a
        ``controls`` list.
    canonical_keys : set[str], optional
        The canonical control keys.  If *None*, loads from
        ``controls.json``.

    Returns
    -------
    list[str]
        Pipeline violation messages for rejected or rewritten IDs.
    """
    if canonical_keys is None:
        canonical_keys = _load_canonical_keys()

    violations: list[str] = []
    total_exact = 0
    total_prefix = 0
    total_reject = 0

    for item in items:
        raw_controls = item.get("controls", [])
        if not raw_controls:
            continue

        normalized: list[str] = []
        item_id = item.get("checklist_id", item.get("initiative_id", "UNKNOWN"))

        for raw_id in raw_controls:
            resolved, status = _resolve_control_id(raw_id, canonical_keys)

            if status == "exact":
                normalized.append(resolved)
                total_exact += 1

            elif status == "prefix":
                normalized.append(resolved)
                total_prefix += 1
                log.info(
                    "Control ID normalized: '%s' → '%s' (item %s)",
                    raw_id, resolved, item_id,
                )
                violations.append(
                    f"CONTROL_ID_NORMALIZED: '{raw_id}' → '{resolved}' "
                    f"in item {item_id} (prefix match)"
                )

            else:  # reject
                total_reject += 1
                log.warning(
                    "Control ID rejected: '%s' in item %s — "
                    "no canonical match found",
                    raw_id, item_id,
                )
                violations.append(
                    f"CONTROL_ID_REJECTED: '{raw_id}' in item {item_id} — "
                    f"no canonical match in controls.json"
                )

        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for cid in normalized:
            if cid not in seen:
                seen.add(cid)
                deduped.append(cid)

        item["controls"] = deduped

    # Summary log
    print(
        f"        → control ID normalization: "
        f"{total_exact} exact, {total_prefix} prefix-resolved, "
        f"{total_reject} rejected"
    )

    return violations


def resolve_item_checklist_ids(
    items: list[dict],
    roadmap_phases: dict | None = None,
    canonical_keys: set[str] | None = None,
) -> list[str]:
    """Resolve synthetic checklist_ids on remediation items to canonical format.

    The AI may emit ``checklist_id`` values like ``"rbac-hygiene-001"`` or UUIDs
    instead of canonical ALZ review-checklist IDs (e.g. ``A01.01``).  This
    function deterministically resolves them using the ``controls[]`` array
    on each item:

      item.controls  →  controls.json  →  checklist_ids  →  A01.01

    Also rewrites matching IDs in ``roadmap_phases`` (30/60/90 day entries)
    so blocker and roadmap references stay consistent.

    Modifies items and roadmap_phases in-place.

    Parameters
    ----------
    items : list[dict]
        Remediation items with ``checklist_id`` and ``controls`` fields.
    roadmap_phases : dict | None
        The ``roadmap_30_60_90`` dict with ``30_days``, ``60_days``,
        ``90_days`` lists of entries that reference ``checklist_id``.
    canonical_keys : set[str] | None
        The canonical control keys from controls.json.  Loaded if *None*.

    Returns
    -------
    list[str]
        Violation/info messages for logging.
    """
    if canonical_keys is None:
        canonical_keys = _load_canonical_keys()

    # Load controls.json for checklist_id lookup
    controls_path = _CONTROLS_JSON_PATH
    try:
        with open(controls_path, encoding="utf-8") as f:
            pack = json.load(f)
        controls_data = pack.get("controls", {})
    except Exception:
        return ["CHECKLIST_RESOLVE_SKIP: could not load controls.json"]

    violations: list[str] = []
    id_remap: dict[str, str] = {}  # old_id → new_canonical_id
    total_resolved = 0
    total_already_valid = 0
    total_unresolvable = 0

    for item in items:
        old_id = item.get("checklist_id", "")
        if not old_id:
            continue

        # Already valid canonical format — skip
        if _CHECKLIST_ID_RE.match(old_id):
            total_already_valid += 1
            continue

        # Resolve via controls[] → controls.json → checklist_ids
        controls_list = item.get("controls", [])
        resolved_cid: str | None = None

        for ctrl_key in controls_list:
            ctrl_data = controls_data.get(ctrl_key, {})
            cids = ctrl_data.get("checklist_ids", [])
            if cids:
                resolved_cid = cids[0]  # take the primary checklist_id
                break

        if resolved_cid and _CHECKLIST_ID_RE.match(resolved_cid):
            id_remap[old_id] = resolved_cid
            item["checklist_id"] = resolved_cid
            item["_original_synthetic_id"] = old_id
            total_resolved += 1
            violations.append(
                f"CHECKLIST_ID_RESOLVED: '{old_id}' → '{resolved_cid}' "
                f"(via control '{controls_list[0]}')"
            )
        else:
            total_unresolvable += 1
            violations.append(
                f"CHECKLIST_ID_UNRESOLVABLE: '{old_id}' has no "
                f"canonical checklist_id mapping from controls {controls_list}"
            )

    # ── Prune hallucinated items ─────────────────────────────────
    # Items with non-canonical IDs AND no controls[] are pure
    # hallucinations — they have zero grounding in the control pack.
    # Remove them before downstream passes see them.
    pruned_count = 0
    kept: list[dict] = []
    for item in items:
        cid = item.get("checklist_id", "")
        if cid and not _CHECKLIST_ID_RE.match(cid) and not item.get("controls"):
            pruned_count += 1
            violations.append(
                f"ITEM_PRUNED: '{cid}' has non-canonical ID and no "
                f"controls[] — removed as hallucinated item."
            )
        else:
            kept.append(item)
    if pruned_count:
        items[:] = kept  # mutate in-place so caller sees the change

    # Rewrite roadmap phase entries to use remapped IDs
    if roadmap_phases and id_remap:
        for phase_key in ("30_days", "60_days", "90_days"):
            entries = roadmap_phases.get(phase_key, [])
            for entry in entries:
                for id_field in ("checklist_id", "initiative_id"):
                    old = entry.get(id_field, "")
                    if old in id_remap:
                        entry[id_field] = id_remap[old]

    # Remove roadmap entries whose IDs are still synthetic/unknown
    # (e.g. UUIDs the AI invented that don't match any item)
    if roadmap_phases:
        valid_item_ids = {i.get("checklist_id") for i in items if i.get("checklist_id")}
        for phase_key in ("30_days", "60_days", "90_days"):
            entries = roadmap_phases.get(phase_key, [])
            cleaned = []
            for entry in entries:
                eid = entry.get("checklist_id", entry.get("initiative_id", ""))
                if eid in valid_item_ids or _CHECKLIST_ID_RE.match(eid):
                    cleaned.append(entry)
                else:
                    violations.append(
                        f"ROADMAP_ENTRY_PRUNED: '{eid}' in {phase_key} "
                        f"has no matching remediation item — removed."
                    )
            roadmap_phases[phase_key] = cleaned

    print(
        f"        → checklist_id resolution: "
        f"{total_already_valid} already valid, {total_resolved} resolved, "
        f"{total_unresolvable} unresolvable, {pruned_count} pruned"
    )

    return violations


def patch_blocker_items(
    readiness: dict | None,
    blocker_mapping: "dict[str, list[str]]",
) -> None:
    """Patch enterprise_scale_readiness blockers with deterministic
    resolving_checklist_ids from the decision_impact blocker mapping.

    Modifies readiness in-place.

    Parameters
    ----------
    readiness : dict
        The enterprise_scale_readiness output (contains ``blockers``).
    blocker_mapping : dict
        Output of ``resolve_blockers_to_items()`` —
        maps blocker category (lowercase) → list of checklist_ids.
    """
    if not blocker_mapping or not readiness:
        return

    blockers = readiness.get("blockers", [])
    for blocker in blockers:
        category = blocker.get("category", "").lower()
        if category in blocker_mapping:
            resolved = blocker_mapping[category]
            if resolved:
                blocker["resolving_checklist_ids"] = resolved
            else:
                # No deterministic match — set empty list + assumption
                blocker["resolving_checklist_ids"] = []
                assumptions = blocker.get("assumptions", [])
                assumptions.append(
                    "No deterministic mapping available — "
                    "no item controls overlap this blocker category."
                )
                blocker["assumptions"] = assumptions


# Keep old name as alias for backward compatibility during transition
patch_blocker_initiatives = patch_blocker_items


# ── Readiness score normalisation ─────────────────────────────────

READINESS_SCORE_MAX = 100


def clamp_readiness_score(readiness: dict | None) -> None:
    """Clamp readiness_score to [0, READINESS_SCORE_MAX] in-place.

    If the raw value exceeds the maximum, it is clamped and an
    assumption note is appended explaining the adjustment.
    """
    if not readiness:
        return
    raw = readiness.get("readiness_score")
    if raw is None:
        return
    if not isinstance(raw, (int, float)):
        return

    clamped = max(0, min(int(raw), READINESS_SCORE_MAX))
    if clamped != int(raw):
        readiness["readiness_score"] = clamped
        assumptions = readiness.setdefault("assumptions", [])
        assumptions.append(
            f"readiness_score clamped from {int(raw)} to {clamped} "
            f"(valid range 0\u2013{READINESS_SCORE_MAX})."
        )
    else:
        readiness["readiness_score"] = clamped


# ── Pipeline validation report ────────────────────────────────────

def validate_pipeline_integrity(
    readiness: dict | None,
    items: list[dict],
    blocker_mapping: "dict[str, list[str]]",
    decision_impact: dict,
) -> list[str]:
    """Run structural integrity checks and return a list of violation strings.

    Prints a summary report during generation.  Returns violations so
    callers can embed them in the output JSON.

    Parameters
    ----------
    readiness : dict | None
        Enterprise-scale readiness output (with blockers).
    items : list[dict]
        Remediation items (each with checklist_id).
    blocker_mapping : dict
        Output of ``resolve_blockers_to_items()`` —
        maps blocker category → list of checklist_ids.
    decision_impact : dict
        Output of ``build_decision_impact_model()``.
    """
    violations: list[str] = []
    valid_ids = {i.get("checklist_id") for i in items if i.get("checklist_id")}

    # \u2500\u2500 1. Blocker \u2192 item referential integrity \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    blockers = (readiness or {}).get("blockers", [])
    valid_blocker_refs = 0
    invalid_blocker_refs = 0

    for b in blockers:
        refs = b.get("resolving_checklist_ids", None)
        # Legacy fallback for old data
        if refs is None:
            legacy = b.get("resolving_item", b.get("resolving_initiative"))
            refs = [legacy] if legacy else []
        if not refs:
            continue  # empty list — unmappable, acceptable
        for ref in refs:
            if ref in valid_ids:
                valid_blocker_refs += 1
            else:
                invalid_blocker_refs += 1
                violations.append(
                    f"Blocker '{b.get('category', '?')}': "
                    f"resolving_checklist_ids entry '{ref}' not in remediation items list."
                )

    # \u2500\u2500 2. Decision impact: controls > 0 implies confidence > 0 \u2500\u2500
    zero_conf_with_controls = 0
    for item in decision_impact.get("items", []):
        controls = item.get("evidence_refs", {}).get("controls", [])
        conf_val = item.get("confidence", {}).get("value", 0.0)
        if len(controls) > 0 and conf_val == 0.0:
            zero_conf_with_controls += 1
            violations.append(
                f"Decision impact '{item.get('checklist_id', '?')}': "
                f"{len(controls)} controls but confidence=0.0."
            )

    # \u2500\u2500 3. Checklist ID format consistency \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    invalid_format = 0
    for i in items:
        cid = i.get("checklist_id", "")
        if cid and not _CHECKLIST_ID_RE.match(cid):
            invalid_format += 1
            violations.append(f"Checklist ID '{cid}' has invalid format (expected e.g. A01.01).")

    # \u2500\u2500 Print report \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    print("\\n  \u2500\u2500 Pipeline Integrity Validation Report \u2500\u2500")
    print(f"    Blockers: {valid_blocker_refs} valid refs, "
          f"{invalid_blocker_refs} invalid refs, "
          f"{sum(1 for b in blockers if not b.get('resolving_checklist_ids'))} unmapped")
    print(f"    Decision impact: {zero_conf_with_controls} items with "
          f"non-empty controls but zero confidence (target: 0)")
    print(f"    Checklist IDs: {len(valid_ids)} total, "
          f"{invalid_format} format violations")
    if violations:
        print(f"    \u26a0 {len(violations)} validation issue(s):")
        for v in violations[:15]:
            print(f"      \u2022 {v}")
    else:
        print("    \u2713 All structural integrity checks passed")

    return violations
