from jinja2 import Environment, FileSystemLoader, select_autoescape
import os, re

from schemas.taxonomy import bucket_domain as _bucket_domain
from schemas.taxonomy import (
    SECTION_TO_DESIGN_AREA,
    OFFICIAL_ALZ_DESIGN_AREAS,
    CHECKLIST_LETTER_TO_DESIGN_AREA,
    DESIGN_AREA_TO_CHECKLIST_LETTER,
    DESIGN_AREA_OBJECTIVES,
)
from engine.scoring import section_scores as _compute_section_scores


# ── Signal-type classification (locked rules) ────────────────────
def _signal_type(ctrl: dict) -> str:
    """Classify a control's signal type.

    Rules (locked):
      - signal_used present and single  → Confirmed
      - signal_used present and comma   → Derived
      - status == 'Manual' AND no signal → Assumed
    """
    sig = ctrl.get("signal_used")
    if sig:
        return "Derived" if "," in str(sig) else "Confirmed"
    if ctrl.get("status") == "Manual":
        return "Assumed"
    return "Assumed"


# ── Confidence badge thresholds ──────────────────────────────────
def _confidence_badge(value) -> str:
    """Map a numeric confidence (0-1) or string to ⚠/Medium/High."""
    if isinstance(value, str):
        val_map = {"high": 1.0, "medium": 0.8, "low": 0.5}
        value = val_map.get(value.lower(), 0.5)
    if value is None:
        return "Unknown"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "Unknown"
    if v < 0.7:
        return "Low"
    if v <= 0.9:
        return "Medium"
    return "High"


def _confidence_numeric(ctrl: dict) -> float:
    """Convert a control's confidence to a float 0-1."""
    c = ctrl.get("confidence")
    if isinstance(c, (int, float)):
        return float(c) if c <= 1 else c / 100.0
    mapping = {"high": 1.0, "medium": 0.8, "low": 0.5}
    return mapping.get(str(c).lower(), 0.5) if c else 0.5


def _domain_for_question(question: dict, results_by_id: dict) -> str:
    """Best-effort domain assignment for a smart question."""
    for key in ("domain", "category"):
        val = question.get(key)
        if val:
            return val
    sections: dict[str, int] = {}
    for cid in question.get("resolves_controls", []):
        ctrl = results_by_id.get(cid)
        if ctrl:
            s = ctrl.get("section", "Other")
            sections[s] = sections.get(s, 0) + 1
    if sections:
        return max(sections, key=sections.get)
    return "General"


# ═════════════════════════════════════════════════════════════════
#  REPORT CONTEXT BUILDER  — 5-Section CSA Decision-Driven Layout
# ═════════════════════════════════════════════════════════════════

def _build_report_context(output: dict) -> dict:
    """
    Derive the 5-section CSA Decision-Driven report context from
    structured JSON fields only.  No scoring-engine changes.

    Sections:
      1. Foundation Gate
      2. Top Business Risks (cards)
      3. 30/60/90 Roadmap
      4. Design Area Breakdown (hierarchical)
      5. Workshop Decision Funnel
    """
    scoring = output.get("scoring", {})
    ai = output.get("ai", {})
    meta = output.get("meta", {})
    exec_ctx = output.get("execution_context", {})
    results = output.get("results", [])
    results_by_id = {r["control_id"]: r for r in results if "control_id" in r}
    auto_cov = scoring.get("automation_coverage", {})

    # Shared lookups
    esr = ai.get("enterprise_scale_readiness", {})
    executive = ai.get("executive", {})
    # Accept new key (remediation_items) with fallback to legacy (initiatives)
    rem_items = ai.get("remediation_items", ai.get("initiatives", []))
    item_by_id = {
        i.get("checklist_id", i.get("initiative_id", "")): i
        for i in rem_items
        if i.get("checklist_id") or i.get("initiative_id")
    }
    roadmap_src = ai.get("transformation_roadmap", {})

    # ── Deterministic models (from 3-layer decision engine) ───────
    dep_graph = ai.get("dependency_graph_model", {})
    risk_impact = ai.get("risk_impact_model", {})
    transform_opt = ai.get("transform_optimization", {})
    deterministic_trajectory = ai.get("deterministic_trajectory", {})

    # Risk impact lookup by checklist_id (fallback initiative_id)
    risk_impact_by_id = {
        item.get("checklist_id", item.get("initiative_id", "")): item
        for item in risk_impact.get("items", [])
    }

    # Use deterministic trajectory if available, else fall back to LLM
    trajectory = deterministic_trajectory if deterministic_trajectory else roadmap_src.get("maturity_trajectory", {})

    # ── 1. FOUNDATION GATE ────────────────────────────────────────
    ready = esr.get("ready_for_enterprise_scale")
    blockers_raw = esr.get("blockers", [])
    min_items = esr.get("minimum_items_required", esr.get("minimum_initiatives_required", []))

    # Deterministic blocker→item mapping (from decision_impact)
    blocker_mapping = ai.get("blocker_item_mapping", ai.get("blocker_initiative_mapping", {}))

    # Re-resolve any empty mapping entries using the latest resolver logic
    # (handles stale cached mappings from before bug fixes)
    empty_keys = [k for k, v in blocker_mapping.items() if not v]
    if empty_keys and blockers_raw and rem_items:
        from engine.decision_impact import resolve_blockers_to_items
        fresh = resolve_blockers_to_items(blockers_raw, rem_items, results)
        for k in empty_keys:
            if fresh.get(k):
                blocker_mapping[k] = fresh[k]

    gate_blockers = []
    for b in blockers_raw:
        blocker_key = b.get("category", "") or b.get("description", "")
        # Prefer deterministic mapping (list), fall back to raw resolving_checklist_ids
        raw_refs = b.get("resolving_checklist_ids", [])
        if not raw_refs:
            # Legacy fallback for old data
            legacy = b.get("resolving_item", b.get("resolving_initiative", ""))
            raw_refs = [legacy] if legacy else []
        mapping_refs = blocker_mapping.get(blocker_key,
                                           blocker_mapping.get(blocker_key.lower(), raw_refs))
        # Normalize: mapping may return a list or a single string (legacy)
        if isinstance(mapping_refs, str):
            resolving_ids = [mapping_refs] if mapping_refs else []
        else:
            resolving_ids = list(mapping_refs) if mapping_refs else []

        # Collect aggregated data across all resolving items
        all_controls: list[str] = []
        all_dep_titles: list[str] = []
        total_controls_resolved = 0
        total_risks_reduced = 0
        resolving_titles: list[str] = []
        item_deps_model = dep_graph.get("item_deps", dep_graph.get("initiative_deps", {}))

        for rid in resolving_ids:
            item = item_by_id.get(rid, {})
            resolving_titles.append(item.get("title", rid))
            item_controls = item.get("controls", [])
            all_controls.extend(item_controls)
            deps = item_deps_model.get(rid, item.get("dependencies", []))
            all_dep_titles.extend(
                item_by_id[d].get("title", d)
                for d in deps if d in item_by_id
            )
            impact_data = risk_impact_by_id.get(rid, {})
            total_controls_resolved += impact_data.get("controls_resolved", 0)
            total_risks_reduced += impact_data.get("risks_reduced", 0)

        # Derive aggregate confidence from all controls
        conf_values = [_confidence_numeric(results_by_id[c])
                       for c in all_controls if c in results_by_id]
        avg_conf = sum(conf_values) / len(conf_values) if conf_values else None

        gate_blockers.append({
            "category": b.get("category", "Unknown"),
            "description": b.get("description", ""),
            "severity": b.get("severity", ""),
            "resolving_checklist_ids": resolving_ids,
            "resolving_initiative_title": " | ".join(resolving_titles) if resolving_titles else "(unmapped)",
            "confidence": _confidence_badge(avg_conf),
            "dependencies": list(dict.fromkeys(all_dep_titles)),  # dedupe, preserve order
            "controls_resolved": total_controls_resolved,
            "risks_reduced": total_risks_reduced,
        })

    # Improvement opportunities (when ready): scaling recommendations
    improvement_opportunities = esr.get("scaling_recommendations", [])

    foundation_gate = {
        "ready": ready,
        "readiness_score": esr.get("readiness_score"),
        "max_subscriptions": esr.get("max_supported_subscriptions_current_state"),
        "blockers": gate_blockers,
        "minimum_items": min_items,
        "improvement_opportunities": improvement_opportunities,
        "overall_maturity": scoring.get("overall_maturity_percent"),
        "automated_controls": auto_cov.get("automated_controls", auto_cov.get("data_driven", 0)),
        "total_controls": auto_cov.get("total_controls", 0),
        "automation_percent": auto_cov.get("automation_percent"),
    }

    # ── 2. TOP BUSINESS RISKS (card layout) ──────────────────────
    raw_risks = executive.get("top_business_risks", [])[:5]

    risk_cards = []
    for risk in raw_risks:
        affected = risk.get("affected_controls", [])
        # Derive design area from majority section of affected controls
        section_counts: dict[str, int] = {}
        ctrl_confs: list[float] = []
        for cid in affected:
            ctrl = results_by_id.get(cid, {})
            sec = ctrl.get("section")
            if sec:
                section_counts[sec] = section_counts.get(sec, 0) + 1
            ctrl_confs.append(_confidence_numeric(ctrl))
        design_area = max(section_counts, key=section_counts.get) if section_counts else "Unknown"
        avg_risk_conf = sum(ctrl_confs) / len(ctrl_confs) if ctrl_confs else None

        # Derive signal type badge (majority of affected controls)
        sig_counts = {"Confirmed": 0, "Derived": 0, "Assumed": 0}
        for cid in affected:
            ctrl = results_by_id.get(cid, {})
            sig_counts[_signal_type(ctrl)] += 1
        signal_badge = max(sig_counts, key=sig_counts.get) if any(sig_counts.values()) else "Assumed"

        # Derive fix item by matching affected_controls ∩ item.controls
        fix_item = None
        for item in rem_items:
            if set(affected) & set(item.get("controls", [])):
                fix_item = {
                    "id": item.get("checklist_id", item.get("initiative_id")),
                    "title": item.get("title"),
                    "blast_radius": item.get("blast_radius"),
                }
                break

        # Score drivers (structured, from metadata)
        status_breakdown = {"Fail": 0, "Partial": 0, "Pass": 0, "Manual": 0}
        severity_set = set()
        for cid in affected:
            ctrl = results_by_id.get(cid, {})
            st = ctrl.get("status", "Manual")
            status_breakdown[st] = status_breakdown.get(st, 0) + 1
            sev = ctrl.get("severity")
            if sev:
                severity_set.add(sev)

        score_drivers = []
        if "Critical" in severity_set or "High" in severity_set:
            score_drivers.append(f"Contains {', '.join(sorted(severity_set))} severity controls")
        score_drivers.append(f"{len(affected)} affected control(s)")
        if status_breakdown.get("Fail", 0):
            score_drivers.append(f"{status_breakdown['Fail']} in Fail state")
        if fix_item:
            score_drivers.append(f"Blast radius: {fix_item['blast_radius']}")

        risk_cards.append({
            "title": risk.get("title", ""),
            "design_area": design_area,
            "risk_level": risk.get("severity", "Medium"),
            "signal_badge": signal_badge,
            "confidence": _confidence_badge(avg_risk_conf),
            "business_impact": risk.get("business_impact", ""),
            "technical_cause": risk.get("technical_cause", ""),
            "score_drivers": score_drivers,
            "fix_item": fix_item,
            "affected_count": len(affected),
        })

    # ── 3. 30/60/90 ROADMAP ──────────────────────────────────────
    # Use dependency-engine-reordered roadmap if available
    dep_phase_assignment = dep_graph.get("phase_assignment", {})
    dep_item_order = dep_graph.get("item_order", dep_graph.get("initiative_order", []))
    dep_item_deps = dep_graph.get("item_deps", dep_graph.get("initiative_deps", {}))

    roadmap_phases = roadmap_src.get("roadmap_30_60_90", {})

    # If dependency engine ran, reorder phases deterministically
    if dep_phase_assignment and dep_item_order:
        # Rebuild roadmap from dependency engine assignments
        reordered_phases: dict[str, list[dict]] = {"30_days": [], "60_days": [], "90_days": []}
        # Collect all roadmap entries by checklist_id (fallback initiative_id)
        all_rm_entries: dict[str, dict] = {}
        for pk in ("30_days", "60_days", "90_days"):
            for entry in roadmap_phases.get(pk, []):
                eid = entry.get("checklist_id", entry.get("initiative_id", ""))
                if eid:
                    all_rm_entries[eid] = entry
        # Place in correct phase per dependency engine
        for cid in dep_item_order:
            target_phase = dep_phase_assignment.get(cid, "90_days")
            entry = all_rm_entries.get(cid)
            if entry:
                reordered_phases[target_phase].append(entry)
        # Add any entries not in the dependency graph
        seen_ids = set(dep_item_order)
        for pk in ("30_days", "60_days", "90_days"):
            for entry in roadmap_phases.get(pk, []):
                eid = entry.get("checklist_id", entry.get("initiative_id", ""))
                if eid and eid not in seen_ids:
                    reordered_phases[pk].append(entry)
        roadmap_phases = reordered_phases

    phase_labels = [
        ("30_days", "0–30 Days", "Foundational blockers"),
        ("60_days", "30–60 Days", "Risk reducers"),
        ("90_days", "60–90 Days", "Optimization"),
    ]

    roadmap_sections = []
    for phase_key, phase_label, phase_desc in phase_labels:
        entries = roadmap_phases.get(phase_key, [])
        phase_entries = []
        for entry in entries:
            cid = entry.get("checklist_id", entry.get("initiative_id", ""))
            item = item_by_id.get(cid, {})
            controls = item.get("controls", [])

            # Use risk impact model for enrichment (deterministic)
            impact = risk_impact_by_id.get(cid, {})
            risks_reduced = impact.get("risks_reduced", 0)
            controls_resolved = impact.get("controls_resolved", 0)
            blast_label = impact.get("blast_radius_label", item.get("blast_radius", ""))

            # Fall back to manual risk counting if risk impact model unavailable
            if not impact and raw_risks:
                risks_reduced = sum(
                    1 for risk in raw_risks
                    if set(risk.get("affected_controls", [])) & set(controls)
                )

            # Dependencies from dependency engine (preferred) or entry
            deps = dep_item_deps.get(cid, entry.get("dependency_on", []))
            dep_titles = [item_by_id.get(d, {}).get("title", d) for d in deps if d in item_by_id]

            # Estimated effort from item.delivery_model
            effort = item.get("delivery_model", {}).get("estimated_duration", "")

            phase_entries.append({
                "checklist_id": cid,
                "title": item.get("title", cid) if item else cid,
                "caf_discipline": entry.get("caf_discipline", item.get("caf_discipline", "")),
                "controls_count": len(controls),
                "controls_resolved": controls_resolved,
                "risks_reduced": risks_reduced,
                "dependencies": dep_titles if dep_titles else deps,
                "estimated_effort": effort,
                "blast_radius": blast_label,
                "owner_role": entry.get("owner_role", item.get("owner_role", "")),
                "success_criteria": entry.get("success_criteria", item.get("success_criteria", "")),
            })

        roadmap_sections.append({
            "key": phase_key,
            "label": phase_label,
            "description": phase_desc,
            "entries": phase_entries,
        })

    # ── 4. DESIGN AREA BREAKDOWN (hierarchical) ──────────────────
    section_scores = _compute_section_scores(results) if results else scoring.get("section_scores", [])

    # Group results by section
    controls_by_section: dict[str, list] = {}
    for r in results:
        sec = r.get("section", "Other")
        controls_by_section.setdefault(sec, []).append(r)

    # Build per-section data, then consolidate into official ALZ areas
    _section_data: dict[str, dict] = {}
    for ss in section_scores:
        sec_name = ss["section"]
        sec_controls = controls_by_section.get(sec_name, [])

        # Per-control enrichment for the controls table
        enriched_controls = []
        conf_values = []
        for ctrl in sec_controls:
            sig_type = _signal_type(ctrl)
            conf_val = _confidence_numeric(ctrl)
            conf_values.append(conf_val)

            # "Why We Think This" from notes + evidence + signal_used
            why_parts = []
            sig = ctrl.get("signal_used")
            if sig:
                why_parts.append(f"Signal: {sig}")
            notes = ctrl.get("notes")
            if notes:
                why_parts.append(notes)
            evidence = ctrl.get("evidence", [])
            if evidence:
                for ev in evidence[:3]:
                    if isinstance(ev, dict):
                        why_parts.append(str(ev.get("detail", ev.get("value", ""))))
                    elif isinstance(ev, str):
                        why_parts.append(ev)

            enriched_controls.append({
                "control_id": ctrl.get("control_id", ""),
                "checklist_ids": ctrl.get("checklist_ids", []),
                "text": ctrl.get("text", ctrl.get("question", "")),
                "status": ctrl.get("status", "Manual"),
                "signal_type": sig_type,
                "why": " · ".join(why_parts) if why_parts else "No signal data available",
                "severity": ctrl.get("severity", ""),
                "confidence": _confidence_badge(conf_val),
            })

        _section_data[sec_name] = {
            "maturity_percent": ss.get("maturity_percent"),
            "counts": ss.get("counts", {}),
            "automated_controls": ss.get("automated_controls", 0),
            "total_controls": ss.get("total_controls", 0),
            "automation_percent": ss.get("automation_percent", 0),
            "critical_fail_count": ss.get("critical_fail_count", 0),
            "critical_partial_count": ss.get("critical_partial_count", 0),
            "conf_values": conf_values,
            "controls": enriched_controls,
        }

    # ── Consolidate sections into official 8 ALZ design areas ─────
    _alz_merged: dict[str, dict] = {}
    for sec_name, data in _section_data.items():
        alz_area = SECTION_TO_DESIGN_AREA.get(sec_name, sec_name)
        if alz_area not in _alz_merged:
            _alz_merged[alz_area] = {
                "section": alz_area,
                "maturity_percents": [],
                "total_controls_sum": 0,
                "automated_controls_sum": 0,
                "critical_fail_count": 0,
                "critical_partial_count": 0,
                "counts": {},
                "conf_values": [],
                "controls": [],
            }
        m = _alz_merged[alz_area]
        if data["maturity_percent"] is not None:
            m["maturity_percents"].append(
                (data["maturity_percent"], data["total_controls"])
            )
        m["total_controls_sum"] += data["total_controls"]
        m["automated_controls_sum"] += data["automated_controls"]
        m["critical_fail_count"] += data["critical_fail_count"]
        m["critical_partial_count"] += data["critical_partial_count"]
        for k, v in data["counts"].items():
            m["counts"][k] = m["counts"].get(k, 0) + v
        m["conf_values"].extend(data["conf_values"])
        m["controls"].extend(data["controls"])

    # Ensure all 8 official areas are present (empty rows for gaps)
    for official in OFFICIAL_ALZ_DESIGN_AREAS:
        if official not in _alz_merged:
            _alz_merged[official] = {
                "section": official,
                "maturity_percents": [],
                "total_controls_sum": 0,
                "automated_controls_sum": 0,
                "critical_fail_count": 0,
                "critical_partial_count": 0,
                "counts": {},
                "conf_values": [],
                "controls": [],
            }

    # Flatten into final design_areas list
    design_areas = []
    for alz_area, m in _alz_merged.items():
        # Weighted-average maturity (by control count)
        if m["maturity_percents"]:
            total_w = sum(w for _, w in m["maturity_percents"])
            if total_w > 0:
                mat_pct = round(
                    sum(p * w for p, w in m["maturity_percents"]) / total_w
                )
            else:
                mat_pct = round(
                    sum(p for p, _ in m["maturity_percents"])
                    / len(m["maturity_percents"])
                )
        else:
            mat_pct = None

        avg_conf = (
            sum(m["conf_values"]) / len(m["conf_values"])
            if m["conf_values"]
            else None
        )

        tc = m["total_controls_sum"]
        ac = m["automated_controls_sum"]
        design_areas.append({
            "section": alz_area,
            "legend_letter": DESIGN_AREA_TO_CHECKLIST_LETTER.get(alz_area, ""),
            "maturity_percent": mat_pct,
            "counts": m["counts"],
            "automated_controls": ac,
            "total_controls": tc,
            "automation_percent": round(ac / tc * 100) if tc else 0,
            "critical_fail_count": m["critical_fail_count"],
            "critical_partial_count": m["critical_partial_count"],
            "confidence": _confidence_badge(avg_conf),
            "controls": m["controls"],
        })

    # Sort by risk: critical fails desc, maturity asc
    design_areas.sort(key=lambda s: (
        -(s.get("critical_fail_count", 0) + s.get("critical_partial_count", 0)),
        s["maturity_percent"] if s["maturity_percent"] is not None else 9999,
        s["section"],
    ))

    # ── 5. WORKSHOP DECISION FUNNEL ──────────────────────────────
    smart_qs = ai.get("smart_questions", [])

    # Build per-domain grouping
    domain_questions: dict[str, list] = {}
    for q in smart_qs:
        domain = _bucket_domain(_domain_for_question(q, results_by_id))
        domain_questions.setdefault(domain, []).append(q)

    # Build per-domain risks and blockers
    domain_risks: dict[str, list] = {}
    for risk in raw_risks:
        for cid in risk.get("affected_controls", []):
            ctrl = results_by_id.get(cid, {})
            sec = ctrl.get("section")
            if sec:
                domain = _bucket_domain(sec)
                domain_risks.setdefault(domain, [])
                if risk not in domain_risks[domain]:
                    domain_risks[domain].append(risk)

    domain_blockers: dict[str, list] = {}
    for b in blockers_raw:
        cat = b.get("category", "")
        domain = _bucket_domain(cat) if cat else "General"
        domain_blockers.setdefault(domain, []).append(b)

    # All domains that have questions, risks, or blockers
    all_domains = sorted(set(
        list(domain_questions.keys()) +
        list(domain_risks.keys()) +
        list(domain_blockers.keys())
    ))

    workshop_funnel = []
    for domain in all_domains:
        d_risks = domain_risks.get(domain, [])[:3]
        d_blockers = domain_blockers.get(domain, [])[:3]
        d_questions = domain_questions.get(domain, [])[:3]

        # Count controls impacted if questions/smart-qs are implemented
        controls_impacted = set()
        for q in domain_questions.get(domain, []):
            controls_impacted.update(q.get("resolves_controls", []))

        workshop_funnel.append({
            "domain": domain,
            "risks": [{"title": r.get("title"), "severity": r.get("severity")} for r in d_risks],
            "blockers": [{"description": b.get("description"), "severity": b.get("severity")} for b in d_blockers],
            "questions": [{
                "question": q.get("question", ""),
                "type": q.get("type", ""),
                "follow_up": q.get("follow_up_recommendation", ""),
                "impact_if_yes": q.get("impact_if_yes", ""),
                "impact_if_no": q.get("impact_if_no", ""),
                "controls_impacted": len(q.get("resolves_controls", [])),
            } for q in d_questions],
            "total_controls_impacted": len(controls_impacted),
        })

    return {
        "foundation_gate": foundation_gate,
        "risk_cards": risk_cards,
        "roadmap_sections": roadmap_sections,
        "trajectory": trajectory if isinstance(trajectory, dict) else {},
        "design_areas": design_areas,
        "checklist_legend": CHECKLIST_LETTER_TO_DESIGN_AREA,
        "design_area_objectives": DESIGN_AREA_OBJECTIVES,
        "workshop_funnel": workshop_funnel,
    }


def generate_report(output: dict, template_name: str = "report_template.html", out_path: str = None):
    # ── Patch stale blocker mappings before integrity check ───────
    # If any blocker has an empty resolving_checklist_ids, re-resolve
    # using the latest decision_impact logic (Strategy 4 fix).
    ai = output.get("ai", {})
    mapping = ai.get("blocker_item_mapping", ai.get("blocker_initiative_mapping", {}))
    esr = ai.get("enterprise_scale_readiness", {})
    blockers = esr.get("blockers", [])
    items = ai.get("remediation_items", ai.get("initiatives", []))
    results = output.get("results", [])
    empty_keys = [k for k, v in mapping.items() if not v]
    if empty_keys and blockers and items:
        from engine.decision_impact import resolve_blockers_to_items
        fresh = resolve_blockers_to_items(blockers, items, results)
        for k in empty_keys:
            if fresh.get(k):
                mapping[k] = fresh[k]
        # Also patch the blocker objects so integrity check sees them
        from engine.id_rewriter import patch_blocker_items
        patch_blocker_items(esr, mapping)

    # ── Relationship integrity gate (Rule F) ──────────────────
    # Abort rendering if structural violations exist.
    from engine.relationship_integrity import validate_relationship_integrity
    ri_ok, ri_violations = validate_relationship_integrity(output)
    if not ri_ok:
        msg = (f"Rendering aborted: {len(ri_violations)} relationship integrity "
               f"violation(s) detected.  Fix violations before generating report.")
        print(f"  \u2717 {msg}")
        for v in ri_violations:
            print(f"    \u2022 {v}")
        # Write a minimal error HTML instead of crashing
        if out_path is None:
            out_path = os.path.join(os.getcwd(), "report.html")
        _write_integrity_error_html(out_path, ri_violations)
        return ri_violations

    base_dir = os.path.dirname(__file__)
    env = Environment(
        loader=FileSystemLoader(base_dir),
        autoescape=select_autoescape(["html", "xml"])
    )

    # Build derived report context and merge
    report_ctx = _build_report_context(output)
    context = {**output, **report_ctx}

    template = env.get_template(template_name)
    html = template.render(**context)

    if out_path is None:
        out_path = os.path.join(os.getcwd(), "report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def _write_integrity_error_html(out_path: str, violations: list[str]) -> None:
    """Write a minimal HTML page listing relationship integrity violations."""
    items = "\n".join(f"<li><code>{v}</code></li>" for v in violations)
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Integrity Error</title>
<style>body{{font-family:sans-serif;max-width:900px;margin:2rem auto;color:#1f2328}}
h1{{color:#cf222e}}code{{background:#f6f8fa;padding:2px 6px;border-radius:3px;font-size:.85rem}}
li{{margin:.4rem 0}}</style></head><body>
<h1>Relationship Integrity Failed</h1>
<p>The report cannot be generated because <strong>{len(violations)}</strong>
structural violation(s) were detected.  Fix these before re-running.</p>
<ol>{items}</ol>
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)