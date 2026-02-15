"""Why-Risk Reasoning Agent — explains WHY a domain is a top risk.

Orchestrates:
  1. Extract the matching risk from executive_summary.top_business_risks
  2. Identify failing controls tied to that risk
  3. Pull dependency impact from the knowledge graph
  4. Find roadmap initiatives that fix those controls
  5. Ground each initiative with Microsoft Learn references
  6. Send the assembled evidence to the reasoning model for causal explanation

Usage:
    from agent.why_reasoning import explain_why
    result = explain_why(run_data, "Networking", provider=provider)
"""
from __future__ import annotations

import json
from typing import Any

from graph.knowledge_graph import ControlKnowledgeGraph
from ai.mcp_retriever import search_docs


# ──────────────────────────────────────────────────────────────────
# Step 1 — Locate the target risk
# ──────────────────────────────────────────────────────────────────

def _find_risk(run: dict, domain: str) -> dict | None:
    """Find the top business risk matching the requested domain.

    Matching strategy (in order):
      1. Exact match on ``domain`` or ``affected_domain`` key
      2. Domain keyword appears in the risk ``title``
      3. Majority of ``affected_controls`` belong to the requested section
    """
    risks = (
        run.get("executive_summary", {}).get("top_business_risks", [])
        or run.get("ai", {}).get("executive", {}).get("top_business_risks", [])
    )
    domain_lower = domain.lower()

    # Build a control-id → section lookup for strategy 3
    section_of: dict[str, str] = {}
    for c in run.get("results", []):
        cid = c.get("control_id", "")
        if cid:
            section_of[cid] = (c.get("section", "") or "").lower()

    for r in risks:
        # Strategy 1 — explicit domain key
        if domain_lower in (r.get("domain", "") or r.get("affected_domain", "")).lower():
            return r
        # Strategy 2 — keyword in title
        if domain_lower in (r.get("title", "") or "").lower():
            return r
        # Strategy 3 — most affected controls are in the requested section
        affected = r.get("affected_controls", [])
        if affected:
            matching = sum(1 for a in affected if domain_lower in section_of.get(a, ""))
            if matching > len(affected) / 2:
                return r
    return None


# ──────────────────────────────────────────────────────────────────
# Step 2 — Collect failing controls
# ──────────────────────────────────────────────────────────────────

def _failing_controls(run: dict, affected_control_ids: list[str]) -> list[dict]:
    """Return controls from the run that failed and are in the affected set."""
    results = run.get("results", [])
    affected_set = set(affected_control_ids)
    return [
        {
            "control_id": c["control_id"],
            "text": c.get("text", ""),
            "section": c.get("section", ""),
            "severity": c.get("severity", ""),
            "status": c.get("status", ""),
            "notes": c.get("notes", ""),
        }
        for c in results
        if c.get("control_id") in affected_set and c.get("status") in ("Fail", "Partial")
    ]


# ──────────────────────────────────────────────────────────────────
# Step 3 — Dependency impact from knowledge graph
# ──────────────────────────────────────────────────────────────────

def _dependency_impact(kg: ControlKnowledgeGraph, control_ids: list[str]) -> list[dict]:
    """For each failing control, find what downstream controls depend on it."""
    impacts = []
    for cid in control_ids:
        # Try short ID (first 8 chars)
        short = cid[:8] if len(cid) > 8 else cid
        dependents = kg.get_dependents(short)
        if dependents:
            node = kg.get_node(short)
            impacts.append({
                "control": short,
                "name": node.name if node else short,
                "blocks": dependents,
                "blocks_count": len(dependents),
            })
    return impacts


# ──────────────────────────────────────────────────────────────────
# Step 4 — Find roadmap initiatives that address these controls
# ──────────────────────────────────────────────────────────────────

def _find_initiatives(run: dict, affected_control_ids: list[str]) -> list[dict]:
    """Find transformation roadmap initiatives tied to affected controls."""
    affected_set = set(affected_control_ids)
    initiatives = (
        run.get("transformation_plan", {}).get("initiatives", [])
        or run.get("ai", {}).get("initiatives", [])
        or []
    )
    matched = []
    for init in initiatives:
        init_controls = set(init.get("controls", []))
        overlap = init_controls & affected_set
        if overlap:
            matched.append({
                "initiative_id": init.get("initiative_id", ""),
                "title": init.get("title", ""),
                "phase": init.get("phase", ""),
                "controls_addressed": list(overlap),
                "priority": init.get("priority", ""),
            })
    return matched


# ──────────────────────────────────────────────────────────────────
# Step 5 — Ground initiatives with Microsoft Learn
# ──────────────────────────────────────────────────────────────────

def _ground_initiatives(initiatives: list[dict]) -> list[dict]:
    """Attach Microsoft Learn references to each initiative."""
    for init in initiatives:
        title = init.get("title", "")
        try:
            refs = search_docs(f"Azure landing zone {title}", top=2)
            init["learn_references"] = [
                {"title": r.get("title", ""), "url": r.get("url", "")}
                for r in refs
            ]
        except Exception:
            init["learn_references"] = []
    return initiatives


# ──────────────────────────────────────────────────────────────────
# Step 6 — Build the prompt and call the reasoning model
# ──────────────────────────────────────────────────────────────────

_WHY_PROMPT = """\
You are a senior Azure Cloud Solution Architect performing a root-cause analysis.

The customer's Azure Landing Zone assessment identified **{domain}** as a top business risk.

## RISK
{risk_json}

## FAILING CONTROLS ({fail_count})
{controls_json}

## DEPENDENCY IMPACT
These failing controls block downstream controls:
{dependencies_json}

## ROADMAP ACTIONS THAT FIX THIS
{initiatives_json}

## INSTRUCTIONS
Produce a JSON object with these keys:
- "domain": the domain name
- "root_cause": A concise 2-3 sentence root-cause analysis explaining WHY this domain is the top risk. Do NOT use task-based language like "Enable X" or "Deploy Y". Describe the current state and its consequences.
- "business_impact": How this risk impacts the customer's business (security posture, compliance, operational reliability). Be specific to the evidence.
- "fix_sequence": An ordered array of objects, each with:
    - "step": integer (execution order)
    - "action": what to do (short imperative)
    - "why_this_order": why this step must come before the next
    - "initiative_id": the roadmap initiative ID (if applicable)
    - "learn_url": the Microsoft Learn URL for this step (from the grounding data)
- "cascade_effect": Describe which downstream controls will automatically improve once the root cause is fixed.

Return ONLY valid JSON. No markdown fences.
"""


def _build_why_prompt(
    domain: str,
    risk: dict,
    controls: list[dict],
    dependencies: list[dict],
    initiatives: list[dict],
) -> str:
    return _WHY_PROMPT.format(
        domain=domain,
        risk_json=json.dumps(risk, indent=2),
        fail_count=len(controls),
        controls_json=json.dumps(controls, indent=2),
        dependencies_json=json.dumps(dependencies, indent=2),
        initiatives_json=json.dumps(initiatives, indent=2),
    )


# ──────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────

def explain_why(
    run: dict,
    domain: str,
    *,
    provider: Any | None = None,
    verbose: bool = True,
) -> dict:
    """
    Explain why *domain* is the top risk.

    Parameters
    ----------
    run : dict
        A loaded run JSON (from out/run-*.json or demo/demo_run.json).
    domain : str
        The domain to explain (e.g. "Networking", "Security", "Governance").
    provider : ReasoningProvider, optional
        If provided, sends the evidence to the AI model for causal explanation.
        If None, returns the raw evidence payload without AI narration.
    verbose : bool
        Print progress to stdout.

    Returns
    -------
    dict with keys: risk, domain, failing_controls, dependency_impact,
    roadmap_actions, and (if provider given) ai_explanation.
    """
    if verbose:
        print(f"\n🔎 Why is {domain} the top risk?")
        print("─" * 50)

    # 1 — Find the risk
    risk = _find_risk(run, domain)
    if not risk:
        available = [
            r.get("domain", r.get("affected_domain", r.get("title", "?")))
            for r in (
                run.get("executive_summary", {}).get("top_business_risks", [])
                or run.get("ai", {}).get("executive", {}).get("top_business_risks", [])
            )
        ]
        return {
            "error": f"No top risk found for domain '{domain}'.",
            "available_domains": available,
        }

    affected = risk.get("affected_controls", [])
    if verbose:
        print(f"  Risk: {risk.get('title', '?')}")
        print(f"  Affected controls: {len(affected)}")

    # 2 — Failing controls
    controls = _failing_controls(run, affected)
    if verbose:
        print(f"  Failing/Partial: {len(controls)}")

    # 3 — Dependency impact
    kg = ControlKnowledgeGraph()
    fail_ids = [c["control_id"] for c in controls]
    deps = _dependency_impact(kg, fail_ids)
    if verbose:
        blocked = sum(d["blocks_count"] for d in deps)
        print(f"  Downstream blocked: {blocked} control(s)")

    # 4 — Roadmap initiatives
    initiatives = _find_initiatives(run, affected)
    if verbose:
        print(f"  Roadmap actions: {len(initiatives)}")

    # 5 — Ground with Learn
    if verbose:
        print("  Grounding initiatives with Microsoft Learn …")
    initiatives = _ground_initiatives(initiatives)

    # Assemble evidence payload
    evidence = {
        "domain": domain,
        "risk": risk,
        "failing_controls": controls,
        "dependency_impact": deps,
        "roadmap_actions": initiatives,
    }

    # 6 — AI explanation (optional)
    if provider is not None:
        if verbose:
            print("  Sending evidence to reasoning model …")
        prompt = _build_why_prompt(domain, risk, controls, deps, initiatives)
        from ai.prompts import PromptPack
        system = PromptPack().system
        template = f"{system}\n---SYSTEM---\n{prompt}"
        try:
            explanation = provider.complete(template, evidence, max_tokens=4000)
            evidence["ai_explanation"] = explanation
            if verbose:
                print("  ✓ AI explanation generated")
        except Exception as e:
            if verbose:
                print(f"  ⚠ AI explanation failed: {e}")
            evidence["ai_explanation"] = {"error": str(e)}
    else:
        if verbose:
            print("  (no AI provider — returning raw evidence)")

    return evidence


# ──────────────────────────────────────────────────────────────────
# Terminal display — judge-friendly formatted output
# ──────────────────────────────────────────────────────────────────

def _short_id(control_id: str) -> str:
    """First 8 chars of a UUID — matches checklist short-ID convention."""
    return control_id[:8] if len(control_id) > 8 else control_id


def print_why_report(result: dict) -> None:
    """Render a rich, human-readable terminal report from explain_why() output."""

    if "error" in result:
        print(f"\n  ⚠  {result['error']}")
        if result.get("available_domains"):
            print("  Available risks:")
            for d in result["available_domains"]:
                print(f"    • {d}")
        return

    domain = result.get("domain", "?").upper()
    ai = result.get("ai_explanation", {})
    risk = result.get("risk", {})
    controls = result.get("failing_controls", [])
    deps = result.get("dependency_impact", [])
    actions = result.get("roadmap_actions", [])

    W = 60  # column width

    # ── Header ────────────────────────────────────────────────────
    print()
    print("╔" + "═" * W + "╗")
    title = f"  {domain} IS THE TOP RISK"
    print("║" + title.ljust(W) + "║")
    print("╚" + "═" * W + "╝")

    # ── Root cause ────────────────────────────────────────────────
    print()
    print("  Root cause:")
    if ai.get("root_cause"):
        # Split AI root cause into bullet-friendly sentences
        for sentence in ai["root_cause"].replace(". ", ".\n").split("\n"):
            sentence = sentence.strip()
            if sentence:
                print(f"    • {sentence}")
    else:
        cause = risk.get("technical_cause", "")
        if cause:
            for part in cause.split(";"):
                part = part.strip()
                if part:
                    print(f"    • {part}")

    # ── Failing controls ──────────────────────────────────────────
    if controls:
        print()
        print("  Failing controls:")
        for c in controls:
            sid = _short_id(c["control_id"])
            status_icon = "✗" if c["status"] == "Fail" else "◑"
            print(f"    {status_icon} {sid} – {c['text']}")
            if c.get("notes"):
                # Truncate long notes for terminal
                note = c["notes"][:120]
                if len(c["notes"]) > 120:
                    note += " …"
                print(f"      └─ {note}")

    # ── Dependency impact ─────────────────────────────────────────
    if deps:
        print()
        print("  Dependency impact:")
        print("  Blocks:")
        # Resolve blocked control names from the knowledge graph
        try:
            kg = ControlKnowledgeGraph()
        except Exception:
            kg = None
        for d in deps:
            for blocked_id in d.get("blocks", []):
                if kg:
                    node = kg.get_node(blocked_id)
                    name = node.name if node else blocked_id
                else:
                    name = blocked_id
                print(f"    ↳ {name}")

    # ── Business impact (AI only) ─────────────────────────────────
    if ai.get("business_impact"):
        print()
        print("  Business impact:")
        print(f"    {ai['business_impact']}")

    # ── Fix sequence / Roadmap actions ────────────────────────────
    fix_seq = ai.get("fix_sequence", [])
    if fix_seq:
        print()
        print("  Roadmap actions:")
        for step in fix_seq:
            n = step.get("step", "?")
            action = step.get("action", "")
            url = step.get("learn_url", "")
            phase_label = _step_to_phase(n, len(fix_seq))
            print(f"    {phase_label} → {action}")
            if step.get("why_this_order"):
                print(f"      Why first: {step['why_this_order'][:120]}")
            if url:
                print(f"      Learn: {url}")
    elif actions:
        # Fallback: raw roadmap actions (no AI)
        print()
        print("  Roadmap actions:")
        for a in actions:
            phase = a.get("phase", "")
            if phase:
                print(f"    {phase} → {a.get('title', '?')}")
            else:
                print(f"    • {a.get('title', '?')}")
            refs = a.get("learn_references", [])
            for ref in refs:
                url = ref.get("url", "")
                if url:
                    print(f"      Learn: {url}")

    # ── Cascade effect (AI only) ──────────────────────────────────
    if ai.get("cascade_effect"):
        print()
        print("  Cascade effect:")
        print(f"    {ai['cascade_effect']}")

    # ── Footer ────────────────────────────────────────────────────
    print()
    print("─" * (W + 2))


def _step_to_phase(step_num: int, total_steps: int) -> str:
    """Map a step number to a 30/60/90 day phase label."""
    if total_steps <= 1:
        return "30 days"
    if total_steps == 2:
        return "30 days" if step_num <= 1 else "60 days"
    # 3+ steps: divide into thirds
    third = total_steps / 3
    if step_num <= third:
        return "30 days"
    elif step_num <= 2 * third:
        return "60 days"
    else:
        return "90 days"