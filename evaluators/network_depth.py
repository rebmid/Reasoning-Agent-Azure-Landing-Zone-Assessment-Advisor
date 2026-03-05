"""Network depth evaluators — VPN/ER gateways, Bastion, WAF, DNS, peering."""
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


# ── VNet Peering Connectivity ────────────────────────────────────
@dataclass
class VNetPeeringEvaluator:
    control_id: str = "vnet-peering-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:vnet_peerings"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:vnet_peerings"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Peering signal unavailable",
                                signals_used=self.required_signals)
        total = (sig.raw or {}).get("total_peerings", 0)
        connected = (sig.raw or {}).get("connected", 0)
        disconnected = (sig.raw or {}).get("disconnected", 0)
        if total == 0:
            return ControlResult(status="Fail", severity="High", confidence="High",
                                reason="No VNet peerings detected. Hub-spoke connectivity not established.",
                                signals_used=self.required_signals)
        if disconnected > 0:
            return ControlResult(status="Partial", severity="High", confidence="High",
                                reason=f"{disconnected}/{total} peering(s) not in Connected state.",
                                evidence=[_to_evidence(p) for p in sig.items if (p.get("peeringState") or "").lower() != "connected"][:5],
                                signals_used=self.required_signals)
        return ControlResult(status="Pass", severity="High", confidence="High",
                            reason=f"All {total} VNet peering(s) in Connected state.",
                            evidence=[_to_evidence(p) for p in sig.items[:5]],
                            signals_used=self.required_signals)

register_evaluator(VNetPeeringEvaluator())


# ── ExpressRoute / VPN Gateway Presence ──────────────────────────
@dataclass
class GatewayPresenceEvaluator:
    control_id: str = "gateway-presence-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:gateway_inventory"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:gateway_inventory"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Gateway signal unavailable",
                                signals_used=self.required_signals)
        vpn = (sig.raw or {}).get("vpn_gateways", 0)
        er = (sig.raw or {}).get("expressroute_gateways", 0)
        circuits = (sig.raw or {}).get("expressroute_circuits", 0)
        if vpn == 0 and er == 0:
            return ControlResult(status="Fail", severity="Medium", confidence="High",
                                reason="No VPN or ExpressRoute gateways detected. Hybrid connectivity not established.",
                                signals_used=self.required_signals)
        parts = []
        if er > 0: parts.append(f"{er} ExpressRoute gateway(s)")
        if vpn > 0: parts.append(f"{vpn} VPN gateway(s)")
        if circuits > 0: parts.append(f"{circuits} ExpressRoute circuit(s)")
        return ControlResult(status="Pass", severity="Medium", confidence="High",
                            reason=f"Hybrid connectivity: {', '.join(parts)}.",
                            evidence=[_to_evidence(g) for g in sig.items[:10]],
                            signals_used=self.required_signals)

register_evaluator(GatewayPresenceEvaluator())


# ── Azure Bastion Presence ───────────────────────────────────────
@dataclass
class BastionPresenceEvaluator:
    control_id: str = "bastion-presence-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:bastion_hosts"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:bastion_hosts"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "Bastion signal unavailable",
                                signals_used=self.required_signals)
        if len(sig.items) == 0:
            return ControlResult(status="Fail", severity="Medium", confidence="High",
                                reason="No Azure Bastion hosts detected. Secure admin access not implemented.",
                                signals_used=self.required_signals)
        return ControlResult(status="Pass", severity="Medium", confidence="High",
                            reason=f"{len(sig.items)} Azure Bastion host(s) deployed.",
                            evidence=[_to_evidence(b) for b in sig.items[:5]],
                            signals_used=self.required_signals)

register_evaluator(BastionPresenceEvaluator())


# ── WAF / Front Door Presence ────────────────────────────────────
@dataclass
class WAFPresenceEvaluator:
    control_id: str = "waf-frontdoor-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:waf_frontdoor"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:waf_frontdoor"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "WAF/FrontDoor signal unavailable",
                                signals_used=self.required_signals)
        app_gw = (sig.raw or {}).get("application_gateways", 0)
        fd = (sig.raw or {}).get("front_doors", 0)
        waf = (sig.raw or {}).get("waf_policies", 0)
        if app_gw == 0 and fd == 0:
            return ControlResult(status="Fail", severity="Medium", confidence="High",
                                reason="No Application Gateway or Front Door detected. WAF protection not implemented.",
                                signals_used=self.required_signals)
        parts = []
        if app_gw > 0: parts.append(f"{app_gw} Application Gateway(s)")
        if fd > 0: parts.append(f"{fd} Front Door(s)")
        if waf > 0: parts.append(f"{waf} WAF policy/ies")
        return ControlResult(status="Pass", severity="Medium", confidence="High",
                            reason=f"Web application protection: {', '.join(parts)}.",
                            evidence=[_to_evidence(i) for i in sig.items[:10]],
                            signals_used=self.required_signals)

register_evaluator(WAFPresenceEvaluator())


# ── Private DNS Zone Coverage ────────────────────────────────────
@dataclass
class PrivateDNSEvaluator:
    control_id: str = "private-dns-001"
    required_signals: list[str] = field(
        default_factory=lambda: ["resource_graph:private_dns_zones"]
    )

    def evaluate(self, ctx: EvalContext, signals: dict[str, SignalResult]) -> ControlResult:
        sig = signals["resource_graph:private_dns_zones"]
        if sig.status != SignalStatus.OK:
            return ControlResult(status="Error", confidence="Low",
                                reason=sig.error_msg or "DNS signal unavailable",
                                signals_used=self.required_signals)
        total = (sig.raw or {}).get("total_zones", 0)
        auto_reg = (sig.raw or {}).get("zones_with_auto_registration", 0)
        if total == 0:
            return ControlResult(status="Fail", severity="Medium", confidence="High",
                                reason="No Private DNS Zones detected. Centralized DNS resolution not implemented.",
                                signals_used=self.required_signals)
        reason = f"{total} Private DNS Zone(s) deployed"
        if auto_reg > 0:
            reason += f", {auto_reg} with auto-registration"
        return ControlResult(status="Pass", severity="Medium", confidence="High",
                            reason=f"{reason}.",
                            evidence=[_to_evidence(z) for z in sig.items[:10]],
                            signals_used=self.required_signals)

register_evaluator(PrivateDNSEvaluator())
