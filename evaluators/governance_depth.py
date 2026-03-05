"""Governance depth evaluators — tags, policy exemptions, custom roles, disk encryption, Advisor."""
from __future__ import annotations

from dataclasses import dataclass, field

from signals.types import ControlResult, EvalContext, SignalResult, SignalStatus
from evaluators.registry import register_evaluator


def _to_evidence(item: dict) -> dict:
    return {
        "type": "resource",
        "resource_id": item.get("id", ""),
        "summary": item.get("name", ""),
        "properties": {k: v for k, v in item.items() if k not in ("id", "name")},
    }


# ── Tag Compliance ───────────────────────────────────────────────
@dataclass
class TagComplianceEvaluator:
    control_id: str = "tag-compliance-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:tag_compliance"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:tag_compliance"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Tag signal unavailable",
                                signals_used=self.required_signals)
        pct = (sig.raw or {}).get("tag_coverage_pct", 0)
        total = (sig.raw or {}).get("total_resources", 0)
        tagged = (sig.raw or {}).get("tagged_resources", 0)
        if pct >= 80:
            return ControlResult(status="Pass", severity="Medium", confidence="High",
                                reason=f"Tag coverage {pct}% ({tagged}/{total} resources tagged).",
                                signals_used=self.required_signals)
        if pct >= 50:
            return ControlResult(status="Partial", severity="Medium", confidence="High",
                                reason=f"Tag coverage {pct}% — below 80% target ({tagged}/{total}).",
                                evidence=[_to_evidence(i) for i in sig.items[:5]],
                                signals_used=self.required_signals)
        return ControlResult(status="Fail", severity="Medium", confidence="High",
                            reason=f"Tag coverage {pct}% — significant gap ({tagged}/{total}).",
                            evidence=[_to_evidence(i) for i in sig.items[:5]],
                            signals_used=self.required_signals)

register_evaluator(TagComplianceEvaluator())


# ── Policy Exemptions ────────────────────────────────────────────
@dataclass
class PolicyExemptionEvaluator:
    control_id: str = "policy-exemptions-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:policy_exemptions"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:policy_exemptions"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Exemption signal unavailable",
                                signals_used=self.required_signals)
        total = (sig.raw or {}).get("total_exemptions", 0)
        waivers = (sig.raw or {}).get("waivers", 0)
        if total == 0:
            return ControlResult(status="Pass", severity="Medium", confidence="High",
                                reason="No policy exemptions found — governance enforcement is clean.",
                                signals_used=self.required_signals)
        if waivers > 5:
            return ControlResult(status="Fail", severity="Medium", confidence="High",
                                reason=f"{waivers} waiver exemptions detected — governance bypass risk.",
                                evidence=[_to_evidence(e) for e in sig.items[:5]],
                                signals_used=self.required_signals)
        return ControlResult(status="Partial", severity="Medium", confidence="High",
                            reason=f"{total} exemption(s) ({waivers} waivers) — review recommended.",
                            evidence=[_to_evidence(e) for e in sig.items[:5]],
                            signals_used=self.required_signals)

register_evaluator(PolicyExemptionEvaluator())


# ── Custom RBAC Roles ────────────────────────────────────────────
@dataclass
class CustomRoleEvaluator:
    control_id: str = "custom-roles-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:custom_roles"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:custom_roles"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Custom role signal unavailable",
                                signals_used=self.required_signals)
        total = (sig.raw or {}).get("total_custom_roles", 0)
        wildcard = (sig.raw or {}).get("wildcard_action_roles", 0)
        if total == 0:
            return ControlResult(status="Pass", severity="Low", confidence="High",
                                reason="No custom RBAC roles — using built-in roles only.",
                                signals_used=self.required_signals)
        if wildcard > 0:
            return ControlResult(status="Fail", severity="High", confidence="High",
                                reason=f"{wildcard}/{total} custom role(s) use wildcard (*) actions — overly broad permissions.",
                                evidence=[_to_evidence(r) for r in sig.items if any("*" in str(a) for a in (r.get("actions") or []))][:5],
                                signals_used=self.required_signals)
        return ControlResult(status="Pass", severity="Low", confidence="High",
                            reason=f"{total} custom role(s) — none use wildcard actions.",
                            evidence=[_to_evidence(r) for r in sig.items[:5]],
                            signals_used=self.required_signals)

register_evaluator(CustomRoleEvaluator())


# ── Disk Encryption ──────────────────────────────────────────────
@dataclass
class DiskEncryptionEvaluator:
    control_id: str = "disk-encryption-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:disk_encryption"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:disk_encryption"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Disk encryption signal unavailable",
                                signals_used=self.required_signals)
        total = (sig.raw or {}).get("total_disks", 0)
        cmk = (sig.raw or {}).get("customer_managed_key", 0)
        if total == 0:
            return ControlResult(status="NotApplicable", confidence="High",
                                reason="No managed disks found.",
                                signals_used=self.required_signals)
        pct = round(cmk / total * 100, 1) if total > 0 else 0
        if cmk == 0:
            return ControlResult(status="Pass", severity="Medium", confidence="High",
                                reason=f"All {total} disk(s) use platform-managed encryption (default).",
                                signals_used=self.required_signals)
        return ControlResult(status="Pass", severity="Medium", confidence="High",
                            reason=f"{cmk}/{total} disk(s) ({pct}%) use customer-managed keys.",
                            evidence=[_to_evidence(d) for d in sig.items[:5]],
                            signals_used=self.required_signals)

register_evaluator(DiskEncryptionEvaluator())


# ── Advisor Recommendations ──────────────────────────────────────
@dataclass
class AdvisorEvaluator:
    control_id: str = "advisor-posture-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["advisor:recommendations"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["advisor:recommendations"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Advisor signal unavailable",
                                signals_used=self.required_signals)
        total = (sig.raw or {}).get("total", 0)
        by_impact = (sig.raw or {}).get("by_impact", {})
        high = by_impact.get("High", 0)
        if total == 0:
            return ControlResult(status="Pass", severity="Low", confidence="Medium",
                                reason="No Advisor recommendations — environment is well-configured.",
                                signals_used=self.required_signals)
        if high > 10:
            return ControlResult(status="Fail", severity="Medium", confidence="High",
                                reason=f"{high} high-impact Advisor recommendations across {total} total.",
                                evidence=[_to_evidence(r) for r in sig.items if r.get("impact") == "High"][:5],
                                signals_used=self.required_signals)
        if high > 0:
            return ControlResult(status="Partial", severity="Medium", confidence="High",
                                reason=f"{high} high-impact Advisor recommendation(s) out of {total} total.",
                                evidence=[_to_evidence(r) for r in sig.items if r.get("impact") == "High"][:5],
                                signals_used=self.required_signals)
        return ControlResult(status="Pass", severity="Low", confidence="High",
                            reason=f"{total} Advisor recommendation(s) — none high-impact.",
                            signals_used=self.required_signals)

register_evaluator(AdvisorEvaluator())


# ── Defender Assessments ─────────────────────────────────────────
@dataclass
class DefenderAssessmentsEvaluator:
    control_id: str = "defender-assessments-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["defender:assessments"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["defender:assessments"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Defender assessments unavailable",
                                signals_used=self.required_signals)
        total = (sig.raw or {}).get("total", 0)
        healthy = (sig.raw or {}).get("healthy", 0)
        unhealthy = (sig.raw or {}).get("unhealthy", 0)
        health_pct = (sig.raw or {}).get("health_pct", 0)
        if total == 0:
            return ControlResult(status="NotApplicable", confidence="Medium",
                                reason="No Defender security assessments found.",
                                signals_used=self.required_signals)
        if health_pct >= 80:
            return ControlResult(status="Pass", severity="High", confidence="High",
                                reason=f"Security assessment health {health_pct}% ({healthy}/{healthy + unhealthy}).",
                                signals_used=self.required_signals)
        if health_pct >= 50:
            return ControlResult(status="Partial", severity="High", confidence="High",
                                reason=f"Security assessment health {health_pct}% — {unhealthy} unhealthy finding(s).",
                                evidence=[_to_evidence(a) for a in sig.items if a.get("status", "").lower() == "unhealthy"][:5],
                                signals_used=self.required_signals)
        return ControlResult(status="Fail", severity="High", confidence="High",
                            reason=f"Security assessment health {health_pct}% — {unhealthy} unhealthy finding(s) of {total}.",
                            evidence=[_to_evidence(a) for a in sig.items if a.get("status", "").lower() == "unhealthy"][:5],
                            signals_used=self.required_signals)

register_evaluator(DefenderAssessmentsEvaluator())
