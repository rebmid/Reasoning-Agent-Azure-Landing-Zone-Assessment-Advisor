# engine/adapter.py — Bridge new evaluators → scoring-compatible result shape
"""
Converts evaluate_control() / evaluate_many() output into the flat list of
dicts that compute_scoring(), rollup_by_section(), most_impactful_gaps(), and
the reporting layer expect.

Foundation Layer 4: all control metadata access uses typed
``ControlDefinition`` attributes — no ``dict[str, Any]`` patterns.

Scoring-compatible shape per result:
    {
        control_id, section, category, question, text,
        status, severity, evidence_count, evidence,
        signal_used, confidence, notes
    }
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from signals.types import EvalScope
from signals.registry import SignalBus
from evaluators.registry import EVALUATORS, evaluate_control
from schemas.taxonomy import DESIGN_AREA_SECTION as _DESIGN_AREA_SECTION, ControlDefinition


# ── Reverse index: evaluator control_id (full_id) → pack short key ────
# Built lazily on first use to avoid import-time work.
_FULLID_INDEX: dict[str, str] = {}


def _build_fullid_index(
    pack_controls: dict[str, ControlDefinition],
) -> dict[str, str]:
    """Build / refresh reverse lookup: ``full_id → pack short key``.

    Called once per assessment run when the first evaluator result is
    adapted.  This replaces the fragile ``control_id[:8]`` convention
    which breaks when the pack key is not the first 8 chars of
    ``full_id`` (e.g. ``netwatch`` vs ``network-watcher-001``).
    """
    return {cd.full_id: key for key, cd in pack_controls.items()}


def _resolve_pack_key(
    control_id: str,
    pack_controls: dict[str, ControlDefinition],
) -> str:
    """Resolve an evaluator control_id to its pack short key.

    Strategy:
      1. Exact match in full_id reverse index (covers all cases)
      2. ``[:8]`` legacy fallback (GUID controls where key == first 8)
    Raises ``KeyError`` if neither lookup succeeds.
    """
    global _FULLID_INDEX  # noqa: PLW0603
    if not _FULLID_INDEX:
        _FULLID_INDEX.update(_build_fullid_index(pack_controls))

    # 1. full_id reverse lookup
    short_key = _FULLID_INDEX.get(control_id)
    if short_key is not None:
        return short_key

    # 2. legacy [:8] fallback
    candidate = control_id[:8]
    if candidate in pack_controls:
        return candidate

    raise KeyError(
        f"Control '{control_id}' not found in pack — neither full_id "
        f"reverse index nor [:8] fallback matched any pack key."
    )


def _section_for_control(
    control_id: str,
    pack_controls: dict[str, ControlDefinition],
) -> str:
    """Resolve scoring section for a control.  No fallback.

    The taxonomy validator guarantees every control has a valid
    ``alz_design_area`` that maps to ``DESIGN_AREA_SECTION``.
    If this raises KeyError the pack was loaded without validation.
    """
    short_key = _resolve_pack_key(control_id, pack_controls)
    meta = pack_controls[short_key]
    return meta.section


def adapt_evaluator_result(
    eval_result: dict[str, Any],
    pack_controls: dict[str, ControlDefinition],
) -> dict[str, Any]:
    """Convert a single evaluate_control() response to scoring shape."""
    control_id = eval_result.get("control_id", "")
    short_key = _resolve_pack_key(control_id, pack_controls)
    meta = pack_controls.get(short_key)

    section = _section_for_control(control_id, pack_controls)
    name = meta.title if meta else control_id
    evidence = eval_result.get("evidence", [])

    # Extract coverage ratio if present
    coverage = eval_result.get("coverage")
    coverage_ratio = None
    if isinstance(coverage, dict):
        coverage_ratio = coverage.get("ratio")
    elif coverage is not None and hasattr(coverage, "ratio"):
        coverage_ratio = getattr(coverage, "ratio", None)

    # Numeric confidence: prefer confidence_score, fall back to label
    confidence_score = eval_result.get("confidence_score")
    if confidence_score is None:
        from signals.types import CONFIDENCE_LABEL
        confidence_score = CONFIDENCE_LABEL.get(eval_result.get("confidence", "High"), 0.7)

    # Severity: evaluator result takes precedence, then pack metadata.
    # Pack metadata is validated at load time — always present.
    severity = eval_result.get("severity") or (meta.severity if meta else "Medium")

    return {
        "control_id": meta.full_id if meta else control_id,
        "category": section,
        "section": section,
        "text": name,
        "question": name,
        "severity": severity,
        "status": eval_result.get("status", "EvaluationError"),
        "evidence_count": len(evidence),
        "evidence": evidence,
        "signal_used": ", ".join(eval_result.get("signals_used", [])) or None,
        "confidence": eval_result.get("confidence", "Low"),
        "confidence_score": round(confidence_score, 2),
        "coverage_ratio": round(coverage_ratio, 4) if coverage_ratio is not None else None,
        "notes": eval_result.get("reason", ""),
        # Checklist grounding (Azure/review-checklists authority)
        "checklist_ids": list(meta.checklist_ids) if meta else [],
        "checklist_guids": list(meta.checklist_guids) if meta else [],
    }


def run_evaluators_for_scoring(
    scope: EvalScope,
    bus: SignalBus,
    *,
    pack_controls: dict[str, ControlDefinition],
    run_id: str = "",
    checklist: dict | None = None,
) -> list[dict[str, Any]]:
    """
    Run all registered evaluators and return scoring-compatible results.

    Parameters
    ----------
    scope : EvalScope
        Tenant + subscriptions being assessed.
    bus : SignalBus
        Signal bus for fetching signal data.
    pack_controls : dict[str, ControlDefinition]
        Typed control definitions from the loaded pack.
    run_id : str
        Unique run identifier.
    checklist : dict | None
        Full ALZ checklist — non-automated items are included as Manual
        so that automation_coverage stays correct.
    """
    # ── Run all evaluators in parallel ─────────────────────────────
    # Signal bus is thread-safe (cache uses a lock) and each evaluator
    # is a pure function that reads signals and returns a result dict.
    # Reset the full_id index so it rebuilds for this pack_controls
    global _FULLID_INDEX  # noqa: PLW0603
    _FULLID_INDEX.clear()

    automated_results: list[dict[str, Any]] = []
    automated_ids: set[str] = set()

    max_workers = min(len(EVALUATORS), 8)
    if max_workers <= 1:
        for cid in EVALUATORS:
            raw = evaluate_control(cid, scope, bus, run_id=run_id)
            adapted = adapt_evaluator_result(raw, pack_controls)
            automated_results.append(adapted)
            automated_ids.add(adapted["control_id"])
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(evaluate_control, cid, scope, bus, run_id=run_id): cid
                for cid in EVALUATORS
            }
            for future in as_completed(futures):
                raw = future.result()
                adapted = adapt_evaluator_result(raw, pack_controls)
                automated_results.append(adapted)
                automated_ids.add(adapted["control_id"])

    # ── Backfill manual items from checklist ──────────────────────
    # Manual items come from the ALZ checklist and are NOT taxonomy-validated.
    # They carry their own category/severity from the checklist source.
    # We tag them clearly so scoring can distinguish them from data-driven controls.
    manual_results: list[dict[str, Any]] = []
    if checklist:
        for item in checklist.get("items", []):
            guid = item.get("guid", "")
            if guid in automated_ids:
                continue
            category = item.get("category") or "Manual"
            manual_results.append({
                "control_id": guid,
                "category": category,
                "section": category,
                "text": item.get("text", ""),
                "question": item.get("text", ""),
                "severity": item.get("severity") or "Medium",
                "status": "Manual",
                "evidence_count": 0,
                "evidence": [],
                "signal_used": None,
                "confidence": "Low",
                "notes": "Manual review required.",
                # Manual items ARE checklist items — self-referencing
                "checklist_ids": [item.get("id", "")] if item.get("id") else [],
                "checklist_guids": [guid] if guid else [],
            })

    return automated_results + manual_results
