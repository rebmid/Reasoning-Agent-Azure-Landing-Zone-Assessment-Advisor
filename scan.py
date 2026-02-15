# scan.py — Azure Landing Zone Assessor
import argparse
import json
import os
from datetime import datetime, timezone

from azure.identity import AzureCliCredential

from alz.loader import load_alz_checklist
from collectors.resource_graph import get_subscriptions
from engine.context import discover_execution_context
from engine.adapter import run_evaluators_for_scoring
from engine.scoring import compute_scoring
from engine.run_store import save_run, get_last_run
from engine.delta import compute_delta
from engine.rollup import rollup_by_section
from reporting.render import generate_report
from reporting.csa_workbook import build_csa_workbook
from ai.engine.reasoning_provider import AOAIReasoningProvider
from ai.engine.reasoning_engine import ReasoningEngine
from ai.prompts import PromptPack
from ai.build_advisor_payload import build_advisor_payload
from preflight.analyzer import run_preflight, build_azure_context, print_preflight_report
from signals.types import EvalScope
from signals.registry import SignalBus
from control_packs.loader import load_pack
from engine.assessment_runtime import AssessmentRuntime
from agent.intent_orchestrator import IntentOrchestrator
from agent.run_loader import load_run
from agent.why_reasoning import build_why_payload, print_why_report
from agent.why_ai import generate_why_explanation

# Import evaluator modules so register_evaluator() calls fire
import evaluators.networking   # noqa: F401
import evaluators.governance   # noqa: F401
import evaluators.security     # noqa: F401

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

        # Step 1: deterministic payload
        payload = build_why_payload(run, args.why, verbose=True)

        # Step 2: optional AI explanation
        if enable_ai and "error" not in payload:
            try:
                provider = AOAIReasoningProvider()
                payload["ai_explanation"] = generate_why_explanation(provider, payload)
            except EnvironmentError as e:
                print(f"  ⚠ AI disabled: {e}")

        # Step 3: terminal display
        print_why_report(payload)

        # Step 4: save JSON + workbook
        os.makedirs(OUT_DIR, exist_ok=True)
        why_path = os.path.join(OUT_DIR, f"why-{args.why.lower()}.json")
        with open(why_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"  Saved: {why_path}")

        # Step 5: generate CSA Workbook with risk-analysis sheet
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
        csa_path = os.path.join(OUT_DIR, "CSA_Workbook_v1.xlsx")
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

    # ── Timing + paths ────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    run_id = now.strftime("run-%Y%m%d-%H%M")
    os.makedirs(OUT_DIR, exist_ok=True)

    run_json_path = os.path.join(OUT_DIR, f"{run_id}.json")
    report_path   = os.path.join(OUT_DIR, "report.html")

    # ── Execution context ─────────────────────────────────────────
    credential = AzureCliCredential(process_timeout=30)
    execution_context = discover_execution_context(credential)
    tenant_id = execution_context.get("tenant_id")

    print(f"  Tenant:          {tenant_id or '(unknown)'}")
    print(f"  Subscriptions:   {execution_context.get('subscription_count_visible', '?')}")
    print(f"  MG access:       {execution_context.get('management_group_access')}")
    print(f"  Identity:        {execution_context.get('identity_type')}")

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
    if args.tenant_wide:
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
    checklist = load_alz_checklist()

    # ── Signal Bus + evaluators ───────────────────────────────────
    print("\nRunning evaluators via SignalBus …")
    scope = EvalScope(
        tenant_id=tenant_id,
        subscription_ids=subscription_ids,
    )
    bus = SignalBus()
    results = run_evaluators_for_scoring(
        scope, bus, run_id=run_id, checklist=checklist,
    )
    scoring = compute_scoring(results)

    auto_count = sum(1 for r in results if r["status"] != "Manual")
    print(f"  Evaluated {auto_count} automated + "
          f"{len(results) - auto_count} manual controls")

    # ── Limitations ───────────────────────────────────────────────
    limitations: list[str] = []
    if not execution_context.get("management_group_access"):
        limitations.append("Management group hierarchy not visible with current access")
    if not subscription_ids:
        limitations.append("No subscriptions visible — assessment is empty")
    # Surface any evaluator-level errors
    for r in results:
        if r.get("status") == "Error":
            limitations.append(
                f"Control {r['control_id'][:8]} error: {r.get('notes', 'unknown')}"
            )

    # ── Build output ──────────────────────────────────────────────
    output: dict = {
        "meta": {
            "tool": "lz-assessor",
            "run_id": run_id,
            "timestamp": now.isoformat(),
            "total_controls": len(results),
            "subscription_ids": subscription_ids,
        },
        "execution_context": execution_context,
        "limitations": limitations,
        "scoring": scoring,
        "rollups": dict(rollup_by_section(results)),
        "results": results,
        "customer_questions": _build_customer_questions(results),
    }
    # ── Build advisor payload ─────────────────────────────────
    print("\nBuilding advisor payload …")
    advisor_payload = build_advisor_payload(
        scoring, results, execution_context,
        delta=output.get("delta"),
    )
    print(f"  Payload: {len(advisor_payload.get('failed_controls', []))} fails, "
          f"{len(advisor_payload.get('sampled_manual_controls', []))} sampled manual")

    # ── AI: Reasoning Engine (optional) ────────────────────────────
    if enable_ai:
        try:
            print("\n╔══════════════════════════════════════╗")
            print("║   Reasoning Engine                   ║")
            print("╚══════════════════════════════════════╝")

            provider = AOAIReasoningProvider()
            engine = ReasoningEngine(provider, PromptPack())
            ai_output = engine.generate(
                advisor_payload,
                run_id=run_id,
                tenant_id=tenant_id or "",
                skip_implementation=False,
            )

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

    # ── Delta from previous run ───────────────────────────────────
    last_run_path = get_last_run(OUT_DIR, tenant_id)
    if last_run_path:
        with open(last_run_path, encoding="utf-8") as f:
            previous = json.load(f)
        output["delta"] = compute_delta(previous, output)
        print(f"  Delta: {output['delta']['count']} control(s) changed since last run.")
    else:
        output["delta"] = {"has_previous": False, "count": 0, "changed_controls": []}

    # ── Persist ───────────────────────────────────────────────────
    with open(run_json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    with open("assessment.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    save_run(OUT_DIR, tenant_id, output)

    # ── Reports ───────────────────────────────────────────────────
    if not args.no_html:
        generate_report(output, out_path=report_path)

    # ── CSA Workbook ──────────────────────────────────────────────
    # Auto-generate why-analysis for each top business risk
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
                if "error" not in wp and enable_ai:
                    try:
                        wp["ai_explanation"] = generate_why_explanation(provider, wp)
                    except Exception:
                        pass   # deterministic payload is still valuable
                why_payloads.append(wp)
            except Exception as e:
                print(f"  ⚠ Why-analysis skipped for {domain}: {e}")
        print(f"  Risk analyses built: {len(why_payloads)}")

    csa_path = os.path.join(OUT_DIR, "CSA_Workbook_v1.xlsx")
    ta_path = os.path.join(OUT_DIR, "target_architecture.json")
    build_csa_workbook(
        run_path=run_json_path,
        target_path=ta_path,
        output_path=csa_path,
        why_payloads=why_payloads or None,
    )

    print(f"\n✓ Done.  {run_json_path}  |  {report_path}  |  {csa_path}")

    if args.pretty:
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
