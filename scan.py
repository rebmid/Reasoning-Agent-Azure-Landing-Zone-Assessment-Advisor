# scan.py — Azure Landing Zone Assessor
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone

# Ensure stdout handles Unicode on Windows terminals that default to cp1252
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from azure.identity import AzureCliCredential

from alz.loader import load_alz_checklist
from collectors.azure_client import set_shared_credential
from collectors.resource_graph import get_subscriptions
from engine.context import discover_execution_context
from engine.adapter import run_evaluators_for_scoring
from engine.scoring import compute_scoring
from engine.aggregation import enrich_results_enterprise, build_scope_summary
from schemas.taxonomy import NON_MATURITY_STATUSES
from engine.run_store import save_run, get_last_run
from engine.delta import compute_delta, compute_trend
from engine.rollup import rollup_by_section
from reporting.render import generate_report
from reporting.csa_workbook import build_csa_workbook, validate_signal_integrity, SignalIntegrityError
from ai.engine.reasoning_provider import AOAIReasoningProvider
from ai.engine.reasoning_engine import ReasoningEngine
from ai.prompts import PromptPack
from ai.build_advisor_payload import build_advisor_payload
from preflight.analyzer import run_preflight, build_azure_context, print_preflight_report
from signals.types import EvalScope
from signals.registry import SignalBus
from signals.telemetry import RunTelemetry
from signals.validation import (
    validate_signal_bindings,
    build_signal_execution_summary,
    print_signal_execution_summary,
    run_validate_signals,
    SignalBindingError,
)
from control_packs.loader import load_pack
from engine.assessment_runtime import AssessmentRuntime
from agent.intent_orchestrator import IntentOrchestrator
from agent.run_loader import load_run
from agent.why_reasoning import build_why_payload, print_why_report
from discovery.resolver import run_workshop

# Import evaluator modules so register_evaluator() calls fire
import evaluators.networking   # noqa: F401
import evaluators.governance   # noqa: F401
import evaluators.security     # noqa: F401
import evaluators.data_protection  # noqa: F401
import evaluators.resilience       # noqa: F401
import evaluators.identity         # noqa: F401
import evaluators.network_coverage # noqa: F401
import evaluators.management       # noqa: F401
import evaluators.cost             # noqa: F401
import evaluators.network_depth    # noqa: F401
import evaluators.governance_depth # noqa: F401

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_customer_questions(results: list[dict]) -> list[dict]:
    """Derive customer questions from manual controls."""
    questions: list[dict] = []
    for r in results:
        if r.get("status") == "Manual":
            q_text = r.get("question") or r.get("text") or r.get("control_id", "")
            questions.append({
                "source": "Manual control",
                "question": q_text,
                "related_controls": [r.get("control_id", "")],
            })
    return questions


def _merge_assumption_questions(
    existing: list[dict],
    target_arch: dict | None,
) -> list[dict]:
    """Append questions from target-architecture assumptions that need confirmation."""
    if not target_arch:
        return existing
    for a in target_arch.get("assumptions", []):
        if a.get("needs_customer_confirmation"):
            existing.append({
                "source": "Assumption",
                "question": a.get("statement") or a.get("description", ""),
                "related_controls": a.get("linked_questions", []),
            })
    return existing

def parse_args():
    p = argparse.ArgumentParser(description="Azure Landing Zone Assessor")
    p.add_argument("--tenant-wide", action="store_true",
                   help="Scan all visible subscriptions (default: Resource Graph subs only)")
    p.add_argument("--no-ai", action="store_true", help="Disable AI narrative")
    p.add_argument("--no-html", action="store_true", help="Skip HTML report")
    p.add_argument("--pretty", action="store_true", help="Pretty-print final JSON to stdout")
    p.add_argument("--preflight", action="store_true",
                   help="Run preflight access probes and exit")
    p.add_argument("--on-demand", metavar="INTENT",
                   help="Run on-demand evaluation via WorkshopAgent (e.g. enterprise_readiness)")
    p.add_argument("--why", metavar="DOMAIN",
                   help="Explain why a domain is a top risk (e.g. Networking, Security)")
    p.add_argument("--demo", action="store_true",
                   help="Run in demo mode using sample data (no Azure connection required)")
    p.add_argument("--workshop", action="store_true",
                   help="Run interactive discovery workshop to resolve Manual controls")
    p.add_argument("--mg-scope", metavar="MG_ID",
                   help="Scope assessment to subscriptions under a specific management group")
    p.add_argument("--validate-signals", action="store_true",
                   help="Probe all signal providers without scoring and exit")
    p.add_argument("--tag", metavar="TAG",
                   help="Label this run snapshot (e.g. 'baseline', 'sprint-3')")
    return p.parse_args()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    args = parse_args()
    enable_ai = not args.no_ai

    print("╔══════════════════════════════════════╗")
    print("║   Azure Landing Zone Assessor        ║")
    print("╚══════════════════════════════════════╝")

    # ── Why-Risk reasoning (runs on existing data — no Azure needed) ──
    if args.why:
        run = load_run(demo=args.demo)

        # Step 1: deterministic payload (no LLM)
        payload = build_why_payload(run, args.why, verbose=True)

        # Step 2: terminal display
        print_why_report(payload)

        # Step 3: save JSON + workbook
        os.makedirs(OUT_DIR, exist_ok=True)
        why_path = os.path.join(OUT_DIR, f"why-{args.why.lower()}.json")
        with open(why_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"  Saved: {why_path}")

        # Step 4: generate CSA Workbook
        # Find the run JSON path for existing workbook data
        run_source = "demo/demo_run.json" if args.demo else None
        if not run_source:
            # Try the latest run file
            from engine.run_store import get_last_run as _glr
            run_source = _glr(OUT_DIR, run.get("execution_context", {}).get("tenant_id"))
        if not run_source:
            # Fall back to assessment.json
            if os.path.exists("assessment.json"):
                run_source = "assessment.json"
        ta_path = os.path.join(OUT_DIR, "target_architecture.json")
        _why_run_id = datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M")
        csa_path = os.path.join(OUT_DIR, f"{_why_run_id}_CSA_Workbook.xlsm")
        if run_source:
            build_csa_workbook(
                run_path=run_source,
                target_path=ta_path,
                output_path=csa_path,
                why_payloads=[payload],
            )

        if args.pretty:
            print(json.dumps(payload, indent=2, default=str))
        return

    # ── Workshop mode (interactive discovery — no Azure needed) ───
    if args.workshop:
        run = load_run(demo=args.demo)
        os.makedirs(OUT_DIR, exist_ok=True)

        # Run the interactive workshop
        updated = run_workshop(run, verbose=True)

        # Persist updated JSON
        ws_path = os.path.join(OUT_DIR, "workshop-run.json")
        with open(ws_path, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, default=str)
        with open("assessment.json", "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, default=str)
        print(f"\n  Workshop run saved: {ws_path}")

        # Re-generate HTML report with updated scoring
        if not args.no_html:
            _ws_run_id = datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M")
            _ws_existing = [f for f in os.listdir(OUT_DIR) if f.startswith("run-") and f.endswith(".json")]
            _ws_snap = len(_ws_existing) + 1
            report_path = os.path.join(OUT_DIR, f"ALZ-Platform-Readiness-Report-{_ws_run_id}-S{_ws_snap:03d}.html")
            generate_report(updated, out_path=report_path)
            print(f"  Report updated:     {report_path}")

        if args.pretty:
            print(json.dumps(updated, indent=2, default=str))
        return

    # ── Timing + paths ────────────────────────────────────────────
    scan_start = time.perf_counter()
    telemetry = RunTelemetry()
    telemetry.mark_live()  # This is a real scan, not demo/cached data
    now = datetime.now(timezone.utc)
    run_id = now.strftime("run-%Y%m%d-%H%M")
    os.makedirs(OUT_DIR, exist_ok=True)

    # Snapshot number: count existing run-*.json files + 1
    _existing = [f for f in os.listdir(OUT_DIR) if f.startswith("run-") and f.endswith(".json")]
    _snap_num = len(_existing) + 1

    run_json_path = os.path.join(OUT_DIR, f"{run_id}.json")
    report_path   = os.path.join(OUT_DIR, f"ALZ-Platform-Readiness-Report-{run_id}-S{_snap_num:03d}.html")

    # ── Execution context ─────────────────────────────────────────
    telemetry.start_phase("context")
    credential = AzureCliCredential(process_timeout=30)
    set_shared_credential(credential)           # all collectors reuse this

    execution_context = discover_execution_context(credential)
    tenant_id = execution_context.get("tenant_id")

    telemetry.subscriptions_visible = execution_context.get("subscription_count_visible", 0)
    telemetry.subscriptions_total = execution_context.get("subscription_count_total", 0)
    telemetry.coverage_percent = execution_context.get("coverage_percent", 0.0)

    tenant_name = execution_context.get("tenant_display_name") or ""
    tenant_domain = execution_context.get("tenant_default_domain") or ""
    tenant_label = f"{tenant_name} ({tenant_id})" if tenant_name else (tenant_id or "(unknown)")
    if tenant_domain:
        tenant_label += f"  [{tenant_domain}]"
    print(f"  Tenant:          {tenant_label}")
    print(f"  Subscriptions:   {execution_context.get('subscription_count_visible', '?')}"
          f" visible / {execution_context.get('subscription_count_total', '?')} total"
          f"  ({execution_context.get('coverage_percent', '?')}% coverage)")
    print(f"  MG access:       {execution_context.get('management_group_access')}")
    print(f"  Credential:      {execution_context.get('credential_method', '?')}")
    print(f"  RBAC role:       {execution_context.get('rbac_highest_role', '?')}")
    print(f"  RBAC scope:      {execution_context.get('rbac_scope', '?')}")
    telemetry.end_phase("context")
    # ── Validate-signals mode (no scoring) ────────────────────
    if args.validate_signals:
        pack = load_pack("alz", "v1.0")
        scope = EvalScope(
            tenant_id=tenant_id,
            subscription_ids=execution_context.get("subscription_ids_visible", []),
        )
        report = run_validate_signals(scope, pack, verbose=True)
        vs_path = os.path.join(OUT_DIR, "signal-validation.json")
        with open(vs_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n  Saved: {vs_path}")
        if report["binding_violations"]:
            print(f"  ⚠ {len(report['binding_violations'])} binding violation(s) found")
        return
    # ── Preflight-only mode ───────────────────────────────────────
    if args.preflight:
        ctx = build_azure_context(
            credential=credential,
            subscription_ids=execution_context.get("subscription_ids_visible", []),
        )
        pf = run_preflight(ctx, verbose=True)
        print_preflight_report(pf)
        pf_path = os.path.join(OUT_DIR, "preflight.json")
        with open(pf_path, "w", encoding="utf-8") as f:
            json.dump(pf, f, indent=2, default=str)
        print(f"  Saved: {pf_path}")
        return

    # ── Subscription list ─────────────────────────────────────────
    if args.mg_scope:
        # Narrow to subscriptions under the specified management group
        import requests as _req
        _token = credential.get_token("https://management.azure.com/.default").token
        try:
            _mg_resp = _req.get(
                f"https://management.azure.com/providers/Microsoft.Management"
                f"/managementGroups/{args.mg_scope}/descendants"
                f"?api-version=2021-04-01",
                headers={"Authorization": f"Bearer {_token}"},
                timeout=20,
            )
            _mg_resp.raise_for_status()
            _mg_subs = {
                d["name"]
                for d in _mg_resp.json().get("value", [])
                if (d.get("type") or "").endswith("/subscriptions")
            }
            # Intersect with visible subscriptions
            all_visible = set(execution_context.get("subscription_ids_visible", []))
            subscription_ids = sorted(all_visible & _mg_subs)
            print(f"\n  --mg-scope {args.mg_scope}: {len(subscription_ids)} subscription(s)"
                  f" (of {len(_mg_subs)} under MG, {len(all_visible)} visible)")
        except Exception as e:
            print(f"  ⚠ --mg-scope lookup failed: {e} — falling back to all visible")
            subscription_ids = execution_context.get("subscription_ids_visible", [])
    elif args.tenant_wide:
        subscription_ids = execution_context.get("subscription_ids_visible", [])
        print(f"\n  Tenant-wide mode: {len(subscription_ids)} subscription(s)")
    else:
        subscription_ids = get_subscriptions()
        print(f"\n  Resource-Graph mode: {len(subscription_ids)} subscription(s)")

    if not subscription_ids:
        print("  ⚠ No subscriptions found — assessment will be limited.")

    # ── On-demand evaluation mode ─────────────────────────────────
    if args.on_demand:
        scope = EvalScope(
            tenant_id=tenant_id,
            subscription_ids=subscription_ids,
        )
        bus = SignalBus()
        pack = load_pack("alz", "v1.0")
        runtime = AssessmentRuntime(bus, pack)

        reasoning: ReasoningEngine | None = None
        if enable_ai:
            provider = AOAIReasoningProvider()
            reasoning = ReasoningEngine(provider, PromptPack())

        orchestrator = IntentOrchestrator(runtime, reasoning)
        result = orchestrator.run_intent(
            args.on_demand, scope,
            run_id=run_id,
            verbose=True,
            skip_reasoning=not enable_ai,
        )
        od_path = os.path.join(OUT_DIR, f"{run_id}-on-demand.json")
        with open(od_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n✓ On-demand assessment saved: {od_path}")
        return

    # ── Checklist (full ALZ list — Manual items backfill scoring) ──
    checklist = load_alz_checklist(force_refresh=True)
    # ── Fail-fast: binding validation ─────────────────────────
    pack = load_pack("alz", "v1.0")
    binding_violations = validate_signal_bindings(pack)
    if binding_violations:
        # Separate critical violations (missing_provider) from expected gaps
        critical = [v for v in binding_violations if v["type"] != "missing_evaluator"]
        pending  = [v for v in binding_violations if v["type"] == "missing_evaluator"]
        if critical:
            print("\n┌─ Signal Binding Errors ───────────────────────────────────┐")
            for v in critical:
                print(f"│  ✗ [{v['type']}] {v['control_id'][:20]}: {v['detail'][:60]}")
            print("└──────────────────────────────────────────────────────────┘")
            raise SignalBindingError(
                f"{len(critical)} critical signal binding violation(s) — "
                f"fix before scanning"
            )
        if pending:
            print(f"\n  ⚠ {len(pending)} data-driven control(s) awaiting evaluator implementation")
    # ── Signal Bus + evaluators ───────────────────────────────────
    telemetry.start_phase("signals")
    print("\nRunning evaluators via SignalBus …")
    scope = EvalScope(
        tenant_id=tenant_id,
        subscription_ids=subscription_ids,
    )
    bus = SignalBus()

    # ── Signal availability matrix ────────────────────────────────
    from signals.availability import probe_signal_availability, print_signal_matrix
    sig_matrix = probe_signal_availability(bus, scope)
    print_signal_matrix(sig_matrix)
    telemetry.end_phase("signals")

    telemetry.start_phase("evaluators")
    results = run_evaluators_for_scoring(
        scope, bus, pack_controls=pack.controls, run_id=run_id, checklist=checklist,
    )
    scoring = compute_scoring(results)

    # ── Enterprise-scale aggregation ──────────────────────────────
    # Enrich each result with coverage %, subscriptions affected,
    # scope level (L1/L2/L3), and scope pattern — NO scoring changes.
    enrich_results_enterprise(results, execution_context)
    scope_summary = build_scope_summary(results)
    print(f"  Scope model: {scope_summary.get('total_findings', 0)} findings, "
          f"{scope_summary.get('governance_gap_percent', 0)}% platform governance gaps")

    # Harvest signal bus telemetry (snapshot events before reset)
    all_bus_events = list(bus.events)  # snapshot for execution summary
    telemetry.record_signal_events(bus.reset_events())

    # ── Signal execution summary (coverage report) ────────────
    sig_summary = build_signal_execution_summary(results, all_bus_events, pack)
    print_signal_execution_summary(sig_summary)

    auto_count = sum(1 for r in results if r["status"] not in NON_MATURITY_STATUSES)
    se_count = sum(1 for r in results if r["status"] == "SignalError")
    ee_count = sum(1 for r in results if r["status"] == "EvaluationError")
    manual_count = sum(1 for r in results if r["status"] == "Manual")
    parts = [f"{auto_count} automated", f"{manual_count} manual"]
    if se_count:
        parts.append(f"{se_count} signal-error")
    if ee_count:
        parts.append(f"{ee_count} eval-error")
    print(f"  Evaluated {' + '.join(parts)} controls")
    telemetry.end_phase("evaluators")

    # ── Limitations ───────────────────────────────────────────────
    limitations: list[str] = []
    if not execution_context.get("management_group_access"):
        limitations.append("Management group hierarchy not visible with current access")
    if not subscription_ids:
        limitations.append("No subscriptions visible — assessment is empty")
    # Surface any evaluator-level errors and signal failures
    from schemas.taxonomy import ERROR_STATUSES as _ERR_STATUSES
    for r in results:
        st = r.get("status")
        if st in _ERR_STATUSES:
            limitations.append(
                f"Control {r['control_id'][:8]} {st}: {r.get('notes', 'unknown')}"
            )

    # ── Build output ──────────────────────────────────────────────
    output: dict = {
        "meta": {
            "tool": "lz-assessor",
            "run_id": run_id,
            "timestamp": now.isoformat(),
            "control_pack_version": pack.version_tag,
            "total_controls": len(results),
            "subscription_ids": subscription_ids,
            **(({"tag": args.tag}) if getattr(args, "tag", None) else {}),
        },
        "execution_context": execution_context,
        "limitations": limitations,
        "signal_availability": sig_matrix,
        "signal_execution_summary": sig_summary,
        "scoring": scoring,
        "scope_summary": scope_summary,
        "rollups": dict(rollup_by_section(results)),
        "results": results,
        "customer_questions": _build_customer_questions(results),
    }
    # ── Build advisor payload ─────────────────────────────────
    print("\nBuilding advisor payload …")
    advisor_payload = build_advisor_payload(
        scoring, results, execution_context,
        delta=output.get("delta"),
        signal_availability=sig_matrix,
    )
    print(f"  Payload: {len(advisor_payload.get('failed_controls', []))} fails, "
          f"{len(advisor_payload.get('sampled_manual_controls', []))} sampled manual")

    # ── AI: Reasoning Engine (optional) ────────────────────────────
    telemetry.start_phase("ai")
    if enable_ai:
        try:
            print("\n╔══════════════════════════════════════╗")
            print("║   Reasoning Engine                   ║")
            print("╚══════════════════════════════════════╝")

            import copy as _copy
            provider = AOAIReasoningProvider()
            engine = ReasoningEngine(provider, PromptPack())
            ai_output = engine.generate(
                _copy.deepcopy(advisor_payload),   # AI gets a copy, never the original
                run_id=run_id,
                tenant_id=tenant_id or "",
                skip_implementation=False,
            )

            # ── Layer 1 immutability guard ─────────────────────────
            # Deep-copy deterministic keys BEFORE AI merge so we can
            # detect both replacement AND in-place mutation.
            _L1_PROTECTED = ("results", "scoring", "rollups",
                             "signal_availability", "signal_execution_summary",
                             "scope_summary", "meta", "execution_context")
            _l1_snapshot = {k: _copy.deepcopy(output[k]) for k in _L1_PROTECTED if k in output}

            # Merge into output
            output["ai"] = ai_output
            output["executive_summary"] = ai_output.get("executive", {})
            output["transformation_plan"] = {
                "initiatives": ai_output.get("initiatives", []),
                "roadmap": ai_output.get("transformation_roadmap", {}),
            }
            output["transformation_roadmap"] = ai_output.get("transformation_roadmap", {})
            output["enterprise_scale_readiness"] = ai_output.get("enterprise_scale_readiness", {})
            output["smart_questions"] = ai_output.get("smart_questions", [])
            output["implementation_backlog"] = ai_output.get("implementation_backlog", [])
            output["progress_analysis"] = ai_output.get("progress_analysis", {})
            output["target_architecture"] = ai_output.get("target_architecture", {})
            output["critical_issues"] = ai_output.get("critical_issues", [])
            _br = ai_output.get("blocker_resolution", {})
            output["blocker_resolution"] = (
                _br.get("blocker_resolution", []) if isinstance(_br, dict) else []
            )

            # Verify Layer 1 keys: no replacement AND no mutation
            for _k, _before in _l1_snapshot.items():
                _after = output.get(_k)
                if _after != _before:
                    raise RuntimeError(
                        f"Layer 1 immutability violation: '{_k}' was "
                        f"{'replaced' if id(_after) != id(_l1_snapshot[_k]) else 'mutated in-place'} "
                        f"during AI merge"
                    )

            # Persist target architecture as standalone artifact
            target_arch = ai_output.get("target_architecture")
            if target_arch:
                ta_path = os.path.join(OUT_DIR, "target_architecture.json")
                with open(ta_path, "w", encoding="utf-8") as f:
                    json.dump(target_arch, f, indent=2)
                print(f"  Target architecture saved: {ta_path}")

            # Merge assumption-sourced questions into customer questions
            output["customer_questions"] = _merge_assumption_questions(
                output.get("customer_questions", []),
                target_arch,
            )

            narrative = ai_output.get("executive", {})
        except EnvironmentError as e:
            print(f"  ⚠ AI skipped: {e}")
            narrative = None
        except Exception as e:
            print(f"  ✗ Reasoning engine failed: {e}")
            narrative = None
    else:
        print("AI disabled (--no-ai).")
        narrative = None
    telemetry.end_phase("ai")

    # ── Delta from previous run ───────────────────────────────────
    last_run_path = get_last_run(OUT_DIR, tenant_id, tenant_name=tenant_name)
    if last_run_path:
        with open(last_run_path, encoding="utf-8") as f:
            previous = json.load(f)
        output["delta"] = compute_delta(previous, output)
        output["trend"] = compute_trend(previous, output)
        print(f"  Delta: {output['delta']['count']} control(s) changed since last run.")
        td = output["trend"]["maturity_delta"]
        print(f"  Trend: maturity {'+' if td >= 0 else ''}{td}pp vs previous run.")
    else:
        output["delta"] = {"has_previous": False, "count": 0, "changed_controls": []}
        output["trend"] = {"has_previous": False}

    # ── Guarantee contract keys (always present, even if AI skipped) ─
    output.setdefault("critical_issues", [])
    output.setdefault("blocker_resolution", [])
    output.setdefault("trend", {"has_previous": False})

    # ── Validate critical_issues schema contract ─────────────────
    # Each issue MUST have: title, design_area, severity, business_risk,
    # recommended_action.  Derive fallbacks from alternate AI field names;
    # drop issues that still have blanks after resolution.
    _CI_REQUIRED = ("title", "design_area", "severity", "business_risk", "recommended_action")
    _CI_FIELD_ALIASES: dict[str, list[str]] = {
        "design_area": ["alz_design_area", "section"],
        "recommended_action": ["course_of_action"],
    }
    _ci_raw = output.get("critical_issues", [])
    _ci_total = len(_ci_raw)
    _ci_valid: list[dict] = []
    for _ci in _ci_raw:
        # Apply aliases: if canonical field missing, try alternates
        for _canon, _alts in _CI_FIELD_ALIASES.items():
            if not _ci.get(_canon):
                for _alt in _alts:
                    _val = _ci.get(_alt)
                    if _val:
                        _ci[_canon] = _val
                        break
        # Coerce list-of-steps → string for recommended_action
        _ra = _ci.get("recommended_action")
        if isinstance(_ra, list):
            _steps = []
            for _s in _ra:
                if isinstance(_s, dict):
                    _steps.append(_s.get("action", str(_s)))
                else:
                    _steps.append(str(_s))
            _ci["recommended_action"] = " → ".join(_steps) if _steps else ""
        # Check all required fields are non-empty strings
        if all(isinstance(_ci.get(f), str) and _ci[f].strip() for f in _CI_REQUIRED):
            _ci_valid.append(_ci)
    _ci_dropped = _ci_total - len(_ci_valid)
    output["critical_issues"] = _ci_valid
    if _ci_dropped:
        print(f"  ⚠ critical_issues: {_ci_dropped}/{_ci_total} dropped (missing required fields)")

    # Telemetry: critical issue validation stats
    output.setdefault("telemetry_extra", {})
    output["telemetry_extra"]["critical_issue_validation"] = {
        "total_generated": _ci_total,
        "valid_after_validation": len(_ci_valid),
        "dropped": _ci_dropped,
    }

    # ── Persist ───────────────────────────────────────────────────
    telemetry.start_phase("reporting")
    output["telemetry"] = telemetry.to_dict()

    # Merge extra telemetry counters (critical_issue_validation, etc.)
    _tex = output.pop("telemetry_extra", {})
    if _tex:
        output["telemetry"].update(_tex)

    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    with open("assessment.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    save_run(OUT_DIR, tenant_id, output, tenant_name=tenant_name)

    # ── Signal integrity gate ─────────────────────────────────────
    # Read back from persisted JSON — renderers must use canonical contract,
    # not in-memory state that may drift after post-write mutations.
    with open(run_json_path, "r", encoding="utf-8") as f:
        persisted_run = json.load(f)

    try:
        provenance = validate_signal_integrity(persisted_run, allow_demo=False)
        print("\n┌─ Signal Integrity ────────────────────┐")
        print(f"│  API calls:         {provenance['api_calls_total']}")
        print(f"│  Signals fetched:   {provenance['signals_fetched']}")
        print(f"│  Data-driven ctrls: {provenance['data_driven_controls']}")
        print("└───────────────────────────────────────┘")
    except SignalIntegrityError as e:
        print(f"\n  ✗ {e}")
        print("  Report generation aborted — no live telemetry collected.")
        return

    # ── Reports ───────────────────────────────────────────────────
    if not args.no_html:
        generate_report(persisted_run, out_path=report_path)

    # ── CSA Workbook ──────────────────────────────────────────────
    # Build deterministic why-payloads for each top business risk
    # (no LLM call — risk scoring and narrative are deterministic)
    why_payloads: list[dict] = []
    top_risks = output.get("executive_summary", {}).get("top_business_risks", [])
    if top_risks:
        print("\nBuilding risk analysis for workbook …")
        for risk in top_risks:
            domain = (
                risk.get("domain")
                or risk.get("affected_domain")
                or risk.get("title", "")
            )
            if not domain:
                continue
            try:
                wp = build_why_payload(output, domain, verbose=False)
                why_payloads.append(wp)
            except Exception as e:
                print(f"  ⚠ Why-analysis skipped for {domain}: {e}")
        print(f"  Risk analyses built: {len(why_payloads)}")

    csa_path = os.path.join(OUT_DIR, f"{run_id}_CSA_Workbook.xlsm")
    ta_path = os.path.join(OUT_DIR, "target_architecture.json")
    build_csa_workbook(
        run_path=run_json_path,
        target_path=ta_path,
        output_path=csa_path,
        why_payloads=why_payloads or None,
    )

    telemetry.end_phase("reporting")

    # ── Final telemetry ───────────────────────────────────────────
    telemetry.assessment_duration_sec = round(time.perf_counter() - scan_start, 2)
    # Update telemetry in persisted JSON
    output["telemetry"] = telemetry.to_dict()
    # Merge MCP grounding telemetry
    from ai.mcp_retriever import get_grounding_telemetry
    output["telemetry"]["grounding"] = get_grounding_telemetry()
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    with open("assessment.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print("\n┌─ Runtime Telemetry ──────────────────┐")
    for line in telemetry.summary_lines():
        print(f"│ {line}")
    print("└──────────────────────────────────────┘")

    print(f"\n✓ Done.  {run_json_path}  |  {report_path}  |  {csa_path}")

    if args.pretty:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
