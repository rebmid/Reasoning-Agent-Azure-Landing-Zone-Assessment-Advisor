from jinja2 import Environment, FileSystemLoader, select_autoescape
import os, re


# ── Capability labels derived from initiative titles ─────────────
_CAPABILITY_MAP = {
    "management group": "Platform management group hierarchy → enables subscription vending & policy inheritance",
    "diagnostics": "Centralized diagnostics & monitoring → enables operational visibility",
    "governance polic": "Governance policy baseline → enables compliant resource provisioning",
    "hub": "Hub connectivity & centralized egress → enables landing zone networking model",
    "spoke": "Hub connectivity & centralized egress → enables landing zone networking model",
    "network": "Hub connectivity & centralized egress → enables landing zone networking model",
    "defender": "Defender for Cloud coverage → enables unified security posture management",
    "security": "Security baseline enforcement → enables SOC integration",
    "identity": "Identity & Entra ID integration → enables PIM and break-glass governance",
    "rbac": "RBAC scoping & delegation → enables subscription-level team autonomy",
    "policy": "Policy-driven guardrails → enables safe self-service for landing zone teams",
    "cost": "Cost governance controls → enables FinOps visibility across landing zones",
    "vending": "Subscription vending automation → enables repeatable landing zone provisioning",
    "automation": "Platform automation & IaC → enables GitOps-driven landing zone lifecycle",
}


def _capability_label(title: str) -> str:
    """Derive a short platform capability label from an initiative title."""
    t = title.lower()
    for keyword, label in _CAPABILITY_MAP.items():
        if keyword in t:
            return label
    return "Platform capability improvement"


def _domain_for_question(question: dict, results_by_id: dict) -> str:
    """Best-effort domain assignment for a smart question."""
    # Check explicit domain/category field first
    for key in ("domain", "category"):
        val = question.get(key)
        if val:
            return val

    # Infer from resolved controls
    sections: dict[str, int] = {}
    for cid in question.get("resolves_controls", []):
        ctrl = results_by_id.get(cid)
        if ctrl:
            s = ctrl.get("section", "Other")
            sections[s] = sections.get(s, 0) + 1
    if sections:
        return max(sections, key=sections.get)
    return "General"


from schemas.taxonomy import (
    bucket_domain as _bucket_domain,
    MODE_SECTIONS as _MODE_SECTIONS,
)


def _build_report_context(output: dict) -> dict:
    """
    Derive all report-specific fields from the raw run JSON.
    Returns a new dict to be merged into the template context.
    """
    scoring = output.get("scoring", {})
    ai = output.get("ai", {})
    meta = output.get("meta", {})
    exec_ctx = output.get("execution_context", {})
    results = output.get("results", [])
    results_by_id = {r["control_id"]: r for r in results if "control_id" in r}

    # ── 1. Platform Readiness Snapshot ────────────────────────────
    esr = ai.get("enterprise_scale_readiness", {})
    executive = ai.get("executive", {})
    overall_score = scoring.get("overall_maturity_percent")
    auto_cov = scoring.get("automation_coverage", {})

    platform_readiness_text = (
        esr.get("summary")
        or executive.get("summary", "")
    )
    # If summary is empty, build from scaling_recommendations
    if not platform_readiness_text:
        recs = esr.get("scaling_recommendations", [])
        if recs:
            platform_readiness_text = " ".join(recs[:3])

    readiness_snapshot = {
        "overall_score": overall_score,
        "automation_percent": auto_cov.get("automation_percent"),
        "automated_controls": auto_cov.get("automated_controls", auto_cov.get("data_driven", 0)),
        "total_controls": auto_cov.get("total_controls", 0),
        "ready_for_enterprise_scale": esr.get("ready_for_enterprise_scale"),
        "readiness_score": esr.get("readiness_score"),
        "max_subscriptions": esr.get("max_supported_subscriptions_current_state"),
        "platform_readiness_text": platform_readiness_text,
    }

    # ── 2. Landing Zone Adoption Blockers ─────────────────────────
    blockers = esr.get("blockers", [])
    initiatives = ai.get("initiatives", [])
    init_by_id = {i["initiative_id"]: i for i in initiatives if "initiative_id" in i}

    lz_blockers = []
    for b in blockers:
        resolving_id = b.get("resolving_initiative", "")
        resolving_init = init_by_id.get(resolving_id, {})
        lz_blockers.append({
            "capability": b.get("category", "Unknown"),
            "description": b.get("description", ""),
            "severity": b.get("severity", ""),
            "remediation_initiative": resolving_init.get("title", resolving_id),
        })

    # Fallback: if no ESR blockers, derive from failing controls grouped by initiative
    if not lz_blockers and initiatives:
        for init in initiatives:
            failing = [results_by_id.get(c, {}) for c in init.get("controls", [])
                       if results_by_id.get(c, {}).get("status") in ("Fail", "Partial")]
            if failing:
                lz_blockers.append({
                    "capability": init.get("caf_discipline", "Unknown"),
                    "description": init.get("why_it_matters", ""),
                    "severity": init.get("blast_radius", ""),
                    "remediation_initiative": init.get("title", ""),
                })

    # ── 3. Highest-Impact Remediation Sequence ────────────────────
    roadmap = ai.get("transformation_roadmap", {})
    dep_graph = roadmap.get("dependency_graph", [])

    # Build a lookup from action prefix to dep_graph entry
    dep_lookup: dict[str, dict] = {}
    for dg in dep_graph if isinstance(dep_graph, list) else []:
        dep_lookup[dg.get("action", "")[:40]] = dg

    initiative_sequence = []
    for init in sorted(initiatives, key=lambda x: x.get("priority", 99)):
        title = init.get("title", "")
        iid = init.get("initiative_id", "")
        depends = init.get("depends_on", [])

        # Try matching dep_graph for phase info
        phase = ""
        for dg in dep_graph if isinstance(dep_graph, list) else []:
            if title[:30].lower() in dg.get("action", "").lower():
                phase = dg.get("phase", "")
                if not depends:
                    depends = dg.get("depends_on", [])
                break

        # Resolve dependency IDs to titles
        dep_titles = []
        for dep in depends:
            if dep in init_by_id:
                dep_titles.append(init_by_id[dep].get("title", dep))
            else:
                dep_titles.append(str(dep)[:60])

        initiative_sequence.append({
            "initiative_id": iid,
            "title": title,
            "priority": init.get("priority"),
            "controls_count": len(init.get("controls", [])),
            "depends_on": dep_titles,
            "capability_unlocked": _capability_label(title),
            "phase": phase,
            "caf_discipline": init.get("caf_discipline", ""),
        })

    # ── 4. Maturity After Roadmap Execution ───────────────────────
    trajectory = roadmap.get("maturity_trajectory", {})

    # ── 5. Capability Unlock View ─────────────────────────────────
    capability_unlock_map = []
    for init in sorted(initiatives, key=lambda x: x.get("priority", 99)):
        title = init.get("title", "")
        capability_unlock_map.append({
            "initiative": title,
            "initiative_id": init.get("initiative_id", ""),
            "capability_enabled": _capability_label(title),
            "alz_design_area": init.get("alz_design_area", init.get("caf_discipline", "General")),
        })

    # ── 6. Domain Deep Dive – assessment modes ────────────────────
    section_scores = [s for s in scoring.get("section_scores", []) if s.get("section") != "Unknown"]
    section_by_name = {s["section"]: s for s in section_scores}

    assessment_modes: dict[str, list] = {}
    for mode, section_list in _MODE_SECTIONS.items():
        mode_sections = []
        for sname in section_list:
            ss = section_by_name.get(sname)
            if ss:
                mode_sections.append(ss)
        assessment_modes[mode] = mode_sections

    # Data Confidence mode: built differently
    # ── Execution context label ───────────────────────────────────
    _id_type = exec_ctx.get("identity_type", "unknown").replace("_", " ").title()
    _cred = exec_ctx.get("credential_method", "")
    _role = exec_ctx.get("rbac_highest_role", "")
    _scope = exec_ctx.get("rbac_scope", "")
    if _cred:
        exec_context_label = f"Delegated {_id_type} via {_cred}"
        exec_context_detail = f"Delegated {_id_type} · {_role or 'Unknown Role'} · {_scope + ' Scope' if _scope else 'Unknown Scope'}"
    else:
        exec_context_label = f"Delegated {_id_type}"
        exec_context_detail = _id_type

    data_confidence = {
        "subscription_count": len(meta.get("subscription_ids", [])),
        "subscription_ids": meta.get("subscription_ids", []),
        "mg_visibility": exec_ctx.get("management_group_access", False),
        "identity_type": exec_ctx.get("identity_type", "Unknown"),
        "exec_context_label": exec_context_label,
        "exec_context_detail": exec_context_detail,
        "tenant_id": exec_ctx.get("tenant_id", ""),
        "tenant_display_name": exec_ctx.get("tenant_display_name", ""),
        "tenant_default_domain": exec_ctx.get("tenant_default_domain", ""),
        "total_controls": auto_cov.get("total_controls", 0),
        "automated_controls": auto_cov.get("automated_controls", auto_cov.get("data_driven", 0)),
        "manual_controls": auto_cov.get("manual_controls", auto_cov.get("requires_customer_input", 0)),
        "automation_percent": auto_cov.get("automation_percent", 0),
        "limitations": output.get("limitations", []),
    }

    # Graph access: infer from whether identity-related signals returned data
    graph_access = False
    for r in results:
        sig = r.get("signal_used") or ""
        if "graph_api" in sig.lower() or "pim" in sig.lower() or "break_glass" in sig.lower():
            if r.get("evidence_count", 0) > 0 or r.get("status") not in ("Manual",):
                graph_access = True
                break
    data_confidence["graph_access"] = graph_access

    # ── Workshop overlay ──────────────────────────────────────────
    workshop = output.get("workshop", {})
    if workshop:
        data_confidence["workshop_applied"] = True
        data_confidence["workshop_completion"] = workshop.get("completion_percent", 0)
        data_confidence["workshop_resolved"] = workshop.get("controls_resolved", 0)
        data_confidence["workshop_remaining"] = workshop.get("manual_remaining",
                                                              data_confidence.get("manual_controls", 0))
        data_confidence["workshop_confidence"] = workshop.get("confidence_level", "Low")
        data_confidence["workshop_questions_answered"] = workshop.get("questions_answered", 0)
        # Recalculate manual controls from workshop perspective
        data_confidence["manual_controls"] = workshop.get("manual_remaining",
                                                           data_confidence.get("manual_controls", 0))
    else:
        data_confidence["workshop_applied"] = False

    # ── 7. Assessment Scope & Confidence (dedicated section) ──────
    # (reuses data_confidence dict above)

    # ── 8. Customer Validation Questions ──────────────────────────
    smart_qs = ai.get("smart_questions", [])
    validation_questions: dict[str, list] = {}
    for q in smart_qs:
        domain = _bucket_domain(_domain_for_question(q, results_by_id))
        validation_questions.setdefault(domain, []).append(q)

    # top business risks
    top_business_risks = executive.get("top_business_risks", [])

    # ALZ design area references from MCP grounding
    alz_design_area_refs = output.get("alz_design_area_references", {})
    alz_design_area_urls = output.get("alz_design_area_urls", {})

    # ── Provenance — scan telemetry for credibility ───────────────
    telemetry = output.get("telemetry", {})
    is_live = telemetry.get("live_run", False)
    sig_avail = output.get("signal_availability", {})

    if is_live:
        rg_queries      = telemetry.get("rg_query_count")
        arm_calls       = telemetry.get("arm_call_count")
        total_api       = (rg_queries or 0) + (arm_calls or 0) if rg_queries is not None or arm_calls is not None else None
        signals_fetched = telemetry.get("signals_fetched")
        signals_cached  = telemetry.get("signals_cached")
        signal_errors   = telemetry.get("signal_errors")
        data_driven_count = auto_cov.get("automated_controls", auto_cov.get("data_driven"))

        signal_inventory: dict[str, int] = {}
        for cat, sigs in sig_avail.items():
            if isinstance(sigs, list):
                signal_inventory[cat] = len(sigs)

        provenance = {
            "live": True,
            "statement": (
                "This report was generated from live platform telemetry. "
                "No questionnaire or Excel input was used."
            ),
            "scan_duration_sec": telemetry.get("assessment_duration_sec"),
            "api_calls_total": total_api,
            "rg_queries": rg_queries,
            "arm_calls": arm_calls,
            "signals_fetched": signals_fetched,
            "signals_cached": signals_cached,
            "signal_errors": signal_errors,
            "signal_inventory": signal_inventory,
            "signal_categories": len(signal_inventory),
            "data_driven_controls": data_driven_count,
            "total_controls": auto_cov.get("total_controls"),
            "phase_context_sec": telemetry.get("phase_context_sec"),
            "phase_signals_sec": telemetry.get("phase_signals_sec"),
            "phase_evaluators_sec": telemetry.get("phase_evaluators_sec"),
            "phase_ai_sec": telemetry.get("phase_ai_sec"),
            "phase_reporting_sec": telemetry.get("phase_reporting_sec"),
        }
    else:
        provenance = {
            "live": False,
            "statement": (
                "Demo Mode \u2014 No live telemetry. "
                "Metrics shown are from cached or sample data."
            ),
        }

    return {
        "readiness_snapshot": readiness_snapshot,
        "lz_blockers": lz_blockers,
        "initiative_sequence": initiative_sequence,
        "trajectory": trajectory if isinstance(trajectory, dict) else {},
        "capability_unlock_map": capability_unlock_map,
        "assessment_modes": assessment_modes,
        "data_confidence": data_confidence,
        "validation_questions": validation_questions,
        "top_business_risks": top_business_risks,
        "platform_readiness_text": platform_readiness_text,
        "workshop": output.get("workshop", {}),
        "alz_design_area_references": alz_design_area_refs,
        "alz_design_area_urls": alz_design_area_urls,
        "provenance": provenance,
    }


def generate_report(output: dict, template_name: str = "report_template.html", out_path: str = None):
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
