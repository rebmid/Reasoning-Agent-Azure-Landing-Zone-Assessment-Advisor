# engine/adapter.py — Bridge new evaluators → scoring-compatible result shape
"""
Converts evaluate_control() / evaluate_many() output into the flat list of
dicts that compute_scoring(), rollup_by_section(), most_impactful_gaps(), and
the reporting layer expect.

Scoring-compatible shape per result:
    {
        control_id, section, category, question, text,
        status, severity, evidence_count, evidence,
        signal_used, confidence, notes
    }
"""
from __future__ import annotations

import json
import os
from typing import Any

from signals.types import EvalScope
from signals.registry import SignalBus
from evaluators.registry import EVALUATORS, evaluate_control
from schemas.taxonomy import DESIGN_AREA_SECTION as _DESIGN_AREA_SECTION

_PACK_CONTROLS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "control_packs", "alz", "v1.0", "controls.json"
)


def _load_pack_controls() -> dict[str, Any]:
    """Load the v1.0 control pack controls.json as a lookup."""
    with open(_PACK_CONTROLS_PATH, encoding="utf-8") as f:
        pack = json.load(f)
    return pack.get("controls", {})


def _section_for_control(control_short_id: str, pack_controls: dict) -> str:
    """Resolve scoring section (Networking/Governance/Security) for a control."""
    meta = pack_controls.get(control_short_id, {})
    area: str = meta.get("design_area", "Unknown")
    return _DESIGN_AREA_SECTION.get(area) or area.title()


def adapt_evaluator_result(
    eval_result: dict[str, Any],
    pack_controls: dict[str, Any],
) -> dict[str, Any]:
    """Convert a single evaluate_control() response to scoring shape."""
    control_id = eval_result.get("control_id", "")
    short_id = control_id[:8]
    meta = pack_controls.get(short_id, {})

    section = _section_for_control(short_id, pack_controls)
    name = meta.get("name", control_id)
    evidence = eval_result.get("evidence", [])

    # Extract coverage ratio if present
    coverage = eval_result.get("coverage")
    coverage_ratio = None
    if isinstance(coverage, dict):
        coverage_ratio = coverage.get("ratio")
    elif hasattr(coverage, "ratio"):
        coverage_ratio = coverage.ratio

    # Numeric confidence: prefer confidence_score, fall back to label
    confidence_score = eval_result.get("confidence_score")
    if confidence_score is None:
        from signals.types import CONFIDENCE_LABEL
        confidence_score = CONFIDENCE_LABEL.get(eval_result.get("confidence", "High"), 0.7)

    return {
        "control_id": meta.get("full_id", control_id),
        "category": section,
        "section": section,
        "text": name,
        "question": name,
        "severity": eval_result.get("severity", meta.get("severity", "Medium")),
        "status": eval_result.get("status", "Unknown"),
        "evidence_count": len(evidence),
        "evidence": evidence,
        "signal_used": ", ".join(eval_result.get("signals_used", [])) or None,
        "confidence": eval_result.get("confidence", "Low"),
        "confidence_score": round(confidence_score, 2),
        "coverage_ratio": round(coverage_ratio, 4) if coverage_ratio is not None else None,
        "notes": eval_result.get("reason", ""),
    }


def run_evaluators_for_scoring(
    scope: EvalScope,
    bus: SignalBus,
    *,
    run_id: str = "",
    checklist: dict | None = None,
) -> list[dict[str, Any]]:
    """
    Run all registered evaluators and return scoring-compatible results.

    If *checklist* is provided (the full ALZ checklist), non-automated items
    are included as Manual so that automation_coverage stays correct.
    """
    pack_controls = _load_pack_controls()

    # ── Run all evaluators via the new architecture ───────────────
    automated_results: list[dict[str, Any]] = []
    automated_ids: set[str] = set()

    for cid in EVALUATORS:
        raw = evaluate_control(cid, scope, bus, run_id=run_id)
        adapted = adapt_evaluator_result(raw, pack_controls)
        automated_results.append(adapted)
        automated_ids.add(adapted["control_id"])

    # ── Backfill manual items from checklist ──────────────────────
    manual_results: list[dict[str, Any]] = []
    if checklist:
        for item in checklist.get("items", []):
            guid = item.get("guid", "")
            if guid in automated_ids:
                continue
            manual_results.append({
                "control_id": guid,
                "category": item.get("category", "Unknown"),
                "section": item.get("category", "Unknown"),
                "text": item.get("text", ""),
                "question": item.get("text", ""),
                "severity": item.get("severity", ""),
                "status": "Manual",
                "evidence_count": 0,
                "evidence": [],
                "signal_used": None,
                "confidence": "Low",
                "notes": "Manual review required.",
            })

    return automated_results + manual_results
