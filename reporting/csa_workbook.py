"""CSA Workbook builder — produces a 6-sheet Excel deliverable from assessment output.

Usage:
    from reporting.csa_workbook import build_csa_workbook
    build_csa_workbook(
        run_path="out/run.json",
        target_path="out/target_architecture.json",
        output_path="out/CSA_Workbook_v1.xlsx",
    )
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _load_json(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_get(obj: Any, dotpath: str, default: Any = "") -> Any:
    """Walk a dot-separated path into nested dicts."""
    for key in dotpath.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key, {})
        else:
            return default
    return obj if obj != {} else default


def _join_list(value) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    return str(value) if value else ""


def _status_fill(status: str | None) -> PatternFill | None:
    s = str(status or "").upper()
    if "PASS" in s:
        return PatternFill("solid", fgColor="C6EFCE")
    if "FAIL" in s:
        return PatternFill("solid", fgColor="FFC7CE")
    if "MANUAL" in s:
        return PatternFill("solid", fgColor="FFEB9C")
    if "PARTIAL" in s:
        return PatternFill("solid", fgColor="FFEB9C")
    return None


# ── Shared styles ─────────────────────────────────────────────────
_BOLD = Font(bold=True)
_HEADER_FILL = PatternFill("solid", fgColor="4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_SECTION_FONT = Font(bold=True, size=12)
_WRAP = Alignment(wrap_text=True, vertical="top")


def _write_header_row(ws, headers: list[str], row: int = 1):
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL


def _auto_width(ws, min_width: int = 12, max_width: int = 55):
    """Set column widths based on content (capped)."""
    for col in ws.columns:
        col_letter = col[0].column_letter
        lengths = [len(str(cell.value or "")) for cell in col]
        width = min(max(max(lengths, default=min_width), min_width), max_width)
        ws.column_dimensions[col_letter].width = width + 2


# ══════════════════════════════════════════════════════════════════
# Risk Analysis sheet builder
# ══════════════════════════════════════════════════════════════════

_SECTION_FILL = PatternFill("solid", fgColor="2F5496")
_SECTION_FONT_WHT = Font(bold=True, size=13, color="FFFFFF")
_SUBSECTION_FILL = PatternFill("solid", fgColor="D6E4F0")
_SUBSECTION_FONT_BLK = Font(bold=True, size=11)
_SEVERITY_FILLS = {
    "High":    PatternFill("solid", fgColor="FFC7CE"),
    "Medium":  PatternFill("solid", fgColor="FFEB9C"),
    "Low":     PatternFill("solid", fgColor="C6EFCE"),
}


def _merge_section(ws, row: int, text: str, max_col: int = 7,
                   fill=None, font=None):
    """Write a merged section header row."""
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=max_col)
    cell = ws.cell(row=row, column=1, value=text)
    cell.font = font or _SECTION_FONT_WHT
    cell.fill = fill or _SECTION_FILL
    cell.alignment = Alignment(vertical="center")


def _merge_text(ws, row: int, text: str, max_col: int = 7):
    """Write a merged multi-line text row."""
    ws.merge_cells(start_row=row, start_column=1,
                   end_row=row, end_column=max_col)
    cell = ws.cell(row=row, column=1, value=text)
    cell.alignment = _WRAP


def _build_risk_analysis_sheet(wb: Workbook, payloads: list[dict]):
    """Add the ``3_Risk_Analysis`` sheet from why-reasoning payloads."""
    ws = wb.create_sheet("3_Risk_Analysis")
    MAX_COL = 7   # merge width
    row = 1

    for idx, payload in enumerate(payloads):
        if "error" in payload:
            continue

        domain = (payload.get("domain") or "Unknown").upper()
        risk = payload.get("risk", {})
        controls = payload.get("failing_controls", [])
        deps = payload.get("dependency_impact", [])
        actions = payload.get("roadmap_actions", [])
        ai = payload.get("ai_explanation", {})

        # ── Domain header ─────────────────────────────────────────
        risk_title = risk.get("title", "")
        _merge_section(ws, row, f"  {domain} — {risk_title}", MAX_COL)
        row += 1

        # ── Root cause ────────────────────────────────────────────
        _merge_section(ws, row, "  Root Cause", MAX_COL,
                       fill=_SUBSECTION_FILL, font=_SUBSECTION_FONT_BLK)
        row += 1
        root_cause = (
            ai.get("root_cause")
            or risk.get("technical_cause", "")
            or risk.get("description", "")
        )
        _merge_text(ws, row, root_cause, MAX_COL)
        row += 2   # blank separator

        # ── Business impact ───────────────────────────────────────
        biz_impact = ai.get("business_impact") or risk.get("business_impact", "")
        if biz_impact:
            _merge_section(ws, row, "  Business Impact", MAX_COL,
                           fill=_SUBSECTION_FILL, font=_SUBSECTION_FONT_BLK)
            row += 1
            _merge_text(ws, row, biz_impact, MAX_COL)
            row += 2

        # ── Failing controls table ────────────────────────────────
        if controls:
            _merge_section(ws, row, "  Failing / Partial Controls", MAX_COL,
                           fill=_SUBSECTION_FILL, font=_SUBSECTION_FONT_BLK)
            row += 1
            ctrl_headers = [
                "Control ID", "Section", "Severity",
                "Status", "Description", "Notes", "",
            ]
            _write_header_row(ws, ctrl_headers, row=row)
            row += 1
            for c in controls:
                cid = c.get("control_id", "")
                short_id = cid[:8] if len(cid) > 8 else cid
                ws.cell(row=row, column=1, value=short_id)
                ws.cell(row=row, column=2, value=c.get("section", ""))
                sev = c.get("severity", "")
                sev_cell = ws.cell(row=row, column=3, value=sev)
                if sev in _SEVERITY_FILLS:
                    sev_cell.fill = _SEVERITY_FILLS[sev]
                status_val = c.get("status", "")
                status_cell = ws.cell(row=row, column=4, value=status_val)
                sfill = _status_fill(status_val)
                if sfill:
                    status_cell.fill = sfill
                ws.cell(row=row, column=5,
                        value=c.get("text", "")).alignment = _WRAP
                ws.cell(row=row, column=6,
                        value=c.get("notes", "")).alignment = _WRAP
                row += 1
            row += 1  # blank separator

        # ── Dependency impact ─────────────────────────────────────
        if deps:
            _merge_section(ws, row, "  Dependency Impact", MAX_COL,
                           fill=_SUBSECTION_FILL, font=_SUBSECTION_FONT_BLK)
            row += 1
            dep_headers = [
                "Failing Control", "Name", "Blocks Count",
                "Blocked Controls", "", "", "",
            ]
            _write_header_row(ws, dep_headers, row=row)
            row += 1
            for d in deps:
                ws.cell(row=row, column=1, value=d.get("control", ""))
                ws.cell(row=row, column=2, value=d.get("name", ""))
                ws.cell(row=row, column=3, value=d.get("blocks_count", 0))
                blocked_str = ", ".join(str(b) for b in d.get("blocks", []))
                ws.cell(row=row, column=4,
                        value=blocked_str).alignment = _WRAP
                row += 1
            row += 1

        # ── Roadmap actions ───────────────────────────────────────
        # Prefer AI fix_sequence when available, fall back to
        # deterministic initiative mapping.
        fix_seq = ai.get("fix_sequence", [])
        if fix_seq:
            _merge_section(ws, row, "  Remediation Roadmap (AI-Prioritized)",
                           MAX_COL, fill=_SUBSECTION_FILL,
                           font=_SUBSECTION_FONT_BLK)
            row += 1
            fix_headers = [
                "Step", "Action", "Why This Order",
                "Phase", "Learn URL", "", "",
            ]
            _write_header_row(ws, fix_headers, row=row)
            row += 1
            total_steps = len(fix_seq)
            for step in fix_seq:
                n = step.get("step", "")
                ws.cell(row=row, column=1, value=n)
                ws.cell(row=row, column=2,
                        value=step.get("action", "")).alignment = _WRAP
                ws.cell(row=row, column=3,
                        value=step.get("why_this_order", "")).alignment = _WRAP
                # Map step to 30/60/90 day phase
                if isinstance(n, int) and total_steps > 0:
                    third = total_steps / 3
                    if n <= third:
                        phase = "30 days"
                    elif n <= 2 * third:
                        phase = "60 days"
                    else:
                        phase = "90 days"
                else:
                    phase = ""
                ws.cell(row=row, column=4, value=phase)
                ws.cell(row=row, column=5,
                        value=step.get("learn_url", "")).alignment = _WRAP
                row += 1
            row += 1
        elif actions:
            _merge_section(ws, row, "  Remediation Roadmap", MAX_COL,
                           fill=_SUBSECTION_FILL, font=_SUBSECTION_FONT_BLK)
            row += 1
            act_headers = [
                "Initiative", "Phase", "Priority",
                "Controls Addressed", "Learn References", "", "",
            ]
            _write_header_row(ws, act_headers, row=row)
            row += 1
            for a in actions:
                ws.cell(row=row, column=1,
                        value=a.get("title", "")).alignment = _WRAP
                ws.cell(row=row, column=2, value=a.get("phase", ""))
                ws.cell(row=row, column=3, value=a.get("priority", ""))
                ws.cell(row=row, column=4,
                        value=_join_list(
                            a.get("controls_addressed", [])
                        )).alignment = _WRAP
                refs = a.get("learn_references", [])
                ref_text = "\n".join(
                    f"{r.get('title', '')}\n{r.get('url', '')}" for r in refs
                )
                ws.cell(row=row, column=5,
                        value=ref_text).alignment = _WRAP
                row += 1
            row += 1

        # ── Cascade effect (AI) ───────────────────────────────────
        cascade = ai.get("cascade_effect", "")
        if cascade:
            _merge_section(ws, row, "  Cascade Effect", MAX_COL,
                           fill=_SUBSECTION_FILL, font=_SUBSECTION_FONT_BLK)
            row += 1
            _merge_text(ws, row, cascade, MAX_COL)
            row += 2

        # ── Separator between domains ─────────────────────────────
        if idx < len(payloads) - 1:
            row += 2   # two blank rows before next domain

    # Column widths
    for col_letter, width in [
        ("A", 16), ("B", 24), ("C", 14), ("D", 18),
        ("E", 55), ("F", 45), ("G", 12),
    ]:
        ws.column_dimensions[col_letter].width = width


# ══════════════════════════════════════════════════════════════════
# Main builder
# ══════════════════════════════════════════════════════════════════

def build_csa_workbook(
    run_path: str = "out/run.json",
    target_path: str = "out/target_architecture.json",
    output_path: str = "out/CSA_Workbook_v1.xlsx",
    why_payloads: list[dict] | None = None,
) -> str:
    """Build the CSA workbook and return the output path.

    Parameters
    ----------
    why_payloads : list[dict], optional
        One or more why-analysis payloads (from ``build_why_payload``).
        Each payload adds a risk-analysis section to the new
        ``3_Risk_Analysis`` sheet in the workbook.
    """

    run = _load_json(run_path)
    target = _load_json(target_path)

    wb = Workbook()
    wb.remove(wb.active)  # type: ignore[arg-type]

    # ── Load ALZ checklist for rich per-control fields ────────────
    checklist_lookup: dict[str, dict] = {}
    try:
        from alz.loader import load_alz_checklist
        cl = load_alz_checklist()
        for item in cl.get("items", []):
            guid = item.get("guid", "")
            if guid:
                checklist_lookup[guid] = item
    except Exception:
        pass  # workbook still works without checklist enrichment

    # ── Build control_id → grounded Learn refs map ────────────────
    grounded_map: dict[str, list[dict]] = {}
    grounded_refs = (
        run.get("ai", {}).get("_raw", {}).get("grounded_refs", [])
    )
    for g in grounded_refs:
        cid = g.get("control_id", "")
        if cid and g.get("references"):
            grounded_map[cid] = g["references"]

    wb = Workbook()
    wb.remove(wb.active)  # type: ignore[arg-type]

    # ── Derived values (safe against missing keys) ────────────────
    tenant_id = _safe_get(run, "execution_context.tenant_id", "Unknown") or "Unknown"
    timestamp = _safe_get(run, "meta.timestamp", datetime.now(timezone.utc).isoformat())
    scoring = run.get("scoring", {})
    coverage = scoring.get("automation_coverage", {})
    maturity = scoring.get("overall_maturity_percent", "")
    data_driven = coverage.get("data_driven", "")
    customer_input = coverage.get("requires_customer_input", "")
    sub_count = _safe_get(run, "execution_context.subscription_count_visible", "")

    es_readiness = run.get("enterprise_scale_readiness", {})
    readiness_label = (
        "Yes" if es_readiness.get("ready_for_enterprise_scale") else "No"
    ) if es_readiness else "Unknown"
    readiness_score = es_readiness.get("readiness_score", "")

    top_risks = run.get("executive_summary", {}).get("top_business_risks", [])
    results = run.get("results", [])

    # =============================================================
    # 0  Executive Summary  (with CSA talking points)
    # =============================================================
    ws = wb.create_sheet("0_Executive_Summary")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 70

    # ── CSA Talking Points ────────────────────────────────────────
    exec_summary = run.get("executive_summary", {})
    exec_narrative = exec_summary.get("executive_narrative", "")
    # Derive engagement framing from executive summary
    risk_areas = ", ".join(
        r.get("title", "")[:60] for r in top_risks[:3]
    ) or "landing zone maturity gaps"

    csa_rows: list[tuple[str, str]] = [
        ("CSA ENGAGEMENT FRAMING", ""),
        ("Engagement Objective",
         "Assess the customer's Azure landing zone maturity, identify "
         "critical gaps, and deliver a prioritised 30-60-90 remediation "
         "roadmap aligned to Microsoft Cloud Adoption Framework."),
        ("Key Message",
         f"This assessment identified {len(results)} controls across "
         f"the tenant. Top risk areas include: {risk_areas}. "
         f"The roadmap ties each action to specific controls and risks, "
         f"making every recommendation defensible and auditable."),
        ("Customer Outcome",
         "A data-driven workbook the customer owns — with scored "
         "controls, a traceable remediation plan, and Microsoft Learn "
         "references — enabling them to drive implementation with or "
         "without further Microsoft engagement."),
        ("", ""),
    ]
    row_num = 1
    for label, val in csa_rows:
        label_cell = ws.cell(row=row_num, column=1, value=label)
        val_cell = ws.cell(row=row_num, column=2, value=val)
        if label == "CSA ENGAGEMENT FRAMING":
            label_cell.font = _SECTION_FONT
        else:
            label_cell.font = _BOLD
            val_cell.alignment = _WRAP
        row_num += 1

    # ── Assessment Metrics ────────────────────────────────────────
    ws.cell(row=row_num, column=1, value="ASSESSMENT METRICS").font = _SECTION_FONT
    row_num += 1

    rows: list[tuple[str, str]] = [
        ("Tenant ID", str(tenant_id)),
        ("Assessment Date", str(timestamp)),
        ("Enterprise-Scale Ready", f"{readiness_label}  (score: {readiness_score})"),
        ("Overall Maturity", f"{maturity}%"),
        ("Data-Driven Controls", str(data_driven)),
        ("Requires Customer Input", str(customer_input)),
        ("Subscriptions Assessed", str(sub_count)),
    ]
    for label, val in rows:
        ws.cell(row=row_num, column=1, value=label).font = _BOLD
        ws.cell(row=row_num, column=2, value=val)
        row_num += 1

    row_num += 1
    ws.cell(row=row_num, column=1, value="Top Risks").font = _SECTION_FONT
    row_num += 1
    _write_header_row(ws, ["Risk", "Business Impact", "Severity"], row_num)
    row_num += 1
    for risk in top_risks[:5]:
        ws.cell(row=row_num, column=1, value=risk.get("title", ""))
        ws.cell(row=row_num, column=2, value=risk.get("business_impact", "")).alignment = _WRAP
        ws.cell(row=row_num, column=3, value=risk.get("severity", ""))
        row_num += 1

    # =============================================================
    # 1  30-60-90 Roadmap  (defensible: each item → controls + risks)
    # =============================================================
    ws = wb.create_sheet("1_30-60-90_Roadmap")
    headers = ["Phase", "Action", "Initiative ID", "CAF Discipline",
               "Owner", "Success Criteria", "Dependencies",
               "Related Controls", "Related Risks"]
    _write_header_row(ws, headers)

    # Build join tables: initiative_id → initiative, and control_guid → risk titles
    initiative_lookup: dict[str, dict] = {}
    for init in run.get("transformation_plan", {}).get("initiatives", []):
        iid = init.get("initiative_id", "")
        if iid:
            initiative_lookup[iid] = init

    # Reverse map: control GUID → set of risk titles
    control_to_risks: dict[str, set[str]] = {}
    for risk in top_risks:
        risk_title = risk.get("title", "")
        for cid in risk.get("affected_controls", []):
            control_to_risks.setdefault(cid, set()).add(risk_title)

    # Control GUID → checklist ID label (e.g. A01.01) for readability
    def _control_labels(guids: list[str]) -> str:
        labels = []
        for g in guids[:8]:
            cl = checklist_lookup.get(g, {})
            labels.append(cl.get("id", g[:8]))
        if len(guids) > 8:
            labels.append(f"+{len(guids) - 8} more")
        return ", ".join(labels)

    def _risks_for_controls(guids: list[str]) -> str:
        risk_titles: set[str] = set()
        for g in guids:
            risk_titles |= control_to_risks.get(g, set())
        return "; ".join(sorted(risk_titles))

    # Prefer transformation_roadmap.roadmap_30_60_90 (has initiative_id),
    # fall back to target_architecture phases if unavailable.
    roadmap_3060 = _safe_get(run, "transformation_roadmap.roadmap_30_60_90", {})
    if roadmap_3060 and isinstance(roadmap_3060, dict):
        row = 2
        for phase_key, phase_label in [
            ("30_days", "30 Days"),
            ("60_days", "60 Days"),
            ("90_days", "90 Days"),
        ]:
            items = roadmap_3060.get(phase_key, [])
            for item in items:
                iid = item.get("initiative_id", "")
                init = initiative_lookup.get(iid, {})
                control_guids = init.get("controls", [])

                ws.cell(row=row, column=1, value=phase_label)
                ws.cell(row=row, column=2, value=item.get("action", "")).alignment = _WRAP
                ws.cell(row=row, column=3, value=iid)
                ws.cell(row=row, column=4, value=item.get("caf_discipline", ""))
                ws.cell(row=row, column=5, value=item.get("owner_role", ""))
                ws.cell(row=row, column=6, value=item.get("success_criteria", "")).alignment = _WRAP
                ws.cell(row=row, column=7, value=_join_list(item.get("dependency_on", []))).alignment = _WRAP
                ws.cell(row=row, column=8, value=_control_labels(control_guids)).alignment = _WRAP
                ws.cell(row=row, column=9, value=_risks_for_controls(control_guids)).alignment = _WRAP
                row += 1
    else:
        # Fallback: target_architecture execution units (no control mapping available)
        row = 2
        for phase in _safe_get(target, "implementation_plan.phases", []):
            phase_name = phase.get("name", phase.get("phase", ""))
            for eu in phase.get("execution_units", []):
                ws.cell(row=row, column=1, value=phase_name)
                ws.cell(row=row, column=2, value=eu.get("capability", ""))
                ws.cell(row=row, column=3, value="")
                ws.cell(row=row, column=4, value="")
                ws.cell(row=row, column=5, value=eu.get("owner", ""))
                ws.cell(row=row, column=6, value=_join_list(eu.get("success_criteria"))).alignment = _WRAP
                ws.cell(row=row, column=7, value=_join_list(eu.get("depends_on"))).alignment = _WRAP
                ws.cell(row=row, column=8, value="")
                ws.cell(row=row, column=9, value="")
                row += 1
    _auto_width(ws)

    # =============================================================
    # 2  Control Details  (enriched from checklist + grounded refs)
    # =============================================================
    ws = wb.create_sheet("2_Control_Details")
    headers = [
        "Checklist ID",    # A — e.g. A01.01
        "Control ID",      # B — guid
        "Category",        # C
        "Subcategory",     # D
        "Description",     # E — full text from checklist
        "WAF Pillar",      # F
        "Service",         # G
        "Status",          # H
        "Severity",        # I
        "Automated",       # J
        "Confidence",      # K
        "Signal Used",     # L
        "Evidence Count",  # M
        "Evidence Summary", # N — first evidence resource IDs
        "Notes",           # O
        "Learn Link",      # P — from checklist
        "Training Link",   # Q — from checklist
        "Grounded References",  # R — from AI grounding
        "Questions to Ask",     # S — from customer_questions + MCP docs
    ]

    # Build control GUID → customer questions lookup
    question_lookup: dict[str, list[str]] = {}
    for cq in run.get("customer_questions", []):
        for cid in cq.get("related_controls", []):
            question_lookup.setdefault(cid, []).append(cq.get("question", ""))
    _write_header_row(ws, headers)

    row = 2
    for c in results:
        cid = c.get("control_id", "")
        cl_item = checklist_lookup.get(cid, {})
        status_val = c.get("status", "")

        # A: Checklist ID (e.g. A01.01)
        ws.cell(row=row, column=1, value=cl_item.get("id", ""))
        # B: GUID
        ws.cell(row=row, column=2, value=cid)
        # C: Category
        ws.cell(row=row, column=3, value=cl_item.get("category", c.get("category", c.get("section", ""))))
        # D: Subcategory
        ws.cell(row=row, column=4, value=cl_item.get("subcategory", ""))
        # E: Full description text
        desc = cl_item.get("text", c.get("text", c.get("question", "")))
        ws.cell(row=row, column=5, value=desc).alignment = _WRAP
        # F: WAF Pillar
        ws.cell(row=row, column=6, value=cl_item.get("waf", ""))
        # G: Service
        ws.cell(row=row, column=7, value=cl_item.get("service", ""))

        # H: Status (with conditional fill)
        status_cell = ws.cell(row=row, column=8, value=status_val)
        fill = _status_fill(status_val)
        if fill:
            status_cell.fill = fill

        # I: Severity
        ws.cell(row=row, column=9, value=c.get("severity", cl_item.get("severity", "")))
        # J: Automated
        automated = "Yes" if status_val in ("Pass", "Fail", "Partial") else "No"
        ws.cell(row=row, column=10, value=automated)
        # K: Confidence
        ws.cell(row=row, column=11, value=c.get("confidence", ""))
        # L: Signal Used
        ws.cell(row=row, column=12, value=c.get("signal_used", ""))
        # M: Evidence Count
        ws.cell(row=row, column=13, value=c.get("evidence_count", 0))

        # N: Evidence Summary — first 3 resource summaries
        evidence = c.get("evidence", [])
        evidence_lines = []
        for ev in evidence[:3]:
            if isinstance(ev, dict):
                summary = ev.get("summary", ev.get("resource_id", ""))
                evidence_lines.append(str(summary)[:120])
        if len(evidence) > 3:
            evidence_lines.append(f"… +{len(evidence) - 3} more")
        ws.cell(row=row, column=14, value="\n".join(evidence_lines)).alignment = _WRAP

        # O: Notes
        ws.cell(row=row, column=15, value=c.get("notes", "")).alignment = _WRAP

        # P: Learn Link (from checklist)
        learn_link = cl_item.get("link", "")
        ws.cell(row=row, column=16, value=learn_link)

        # Q: Training Link (from checklist)
        training_link = cl_item.get("training", "")
        ws.cell(row=row, column=17, value=training_link)

        # R: Grounded References (from AI MCP grounding)
        grounded = grounded_map.get(cid, [])
        if grounded:
            ref_lines = []
            for ref in grounded[:3]:
                title = ref.get("title", "")
                url = ref.get("url", "")
                ref_lines.append(f"{title}\n{url}" if url else title)
            ws.cell(row=row, column=18, value="\n\n".join(ref_lines)).alignment = _WRAP

        # S: Questions to Ask (from customer_questions mapped to this control)
        qs = question_lookup.get(cid, [])
        if qs:
            ws.cell(row=row, column=19, value="\n\n".join(
                f"• {q}" for q in qs[:5]
            )).alignment = _WRAP

        row += 1

    # Auto-size key columns, cap wide ones
    for col_letter, width in [
        ("A", 12), ("B", 14), ("C", 22), ("D", 22), ("E", 50),
        ("F", 14), ("G", 14), ("H", 10), ("I", 10), ("J", 10),
        ("K", 12), ("L", 28), ("M", 8), ("N", 40), ("O", 40),
        ("P", 45), ("Q", 45), ("R", 50), ("S", 55),
    ]:
        ws.column_dimensions[col_letter].width = width

    # =============================================================
    # 3  Risk Analysis  (from why-reasoning payloads)
    # =============================================================
    if why_payloads:
        _build_risk_analysis_sheet(wb, why_payloads)

    # ── Save (with fallback if file is locked) ─────────────────────
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        wb.save(output_path)
    except PermissionError:
        stem = Path(output_path).stem
        ts = datetime.now().strftime("%H%M%S")
        fallback = Path(output_path).with_name(f"{stem}_{ts}.xlsx")
        wb.save(str(fallback))
        print(f"  ⚠ {Path(output_path).name} is locked (open in Excel?). Saved as {fallback.name}")
        output_path = str(fallback)
    print(f"  ✓ CSA workbook → {output_path}  ({len(results)} controls, "
          f"{len(target.get('assumptions', []))} assumptions)")
    return output_path
