"""Signal availability matrix — runtime diagnostic showing which signals are reachable."""
from __future__ import annotations

from typing import Any

from signals.types import EvalScope, SignalStatus
from signals.registry import SIGNAL_PROVIDERS, SignalBus


# Signals grouped by source for display
SIGNAL_SOURCES: dict[str, list[str]] = {
    "Resource Graph": [
        "resource_graph:azure_firewall",
        "resource_graph:vnets",
        "resource_graph:public_ips",
        "resource_graph:route_tables",
        "resource_graph:nsgs",
        "resource_graph:storage_posture",
        "resource_graph:keyvault_posture",
        "resource_graph:sql_posture",
        "resource_graph:app_service_posture",
        "resource_graph:acr_posture",
        "resource_graph:aks_posture",
        "resource_graph:private_endpoints",
        "resource_graph:nsg_coverage",
        "resource_graph:resource_locks",
        "resource_graph:backup_coverage",
        "resource_graph:vnet_peerings",
        "resource_graph:gateway_inventory",
        "resource_graph:bastion_hosts",
        "resource_graph:waf_frontdoor",
        "resource_graph:private_dns_zones",
        "resource_graph:tag_compliance",
        "resource_graph:disk_encryption",
        "resource_graph:custom_roles",
        "resource_graph:policy_exemptions",
    ],
    "ARM / Management": [
        "arm:mg_hierarchy",
    ],
    "Policy": [
        "policy:assignments",
        "policy:compliance_summary",
    ],
    "Defender": [
        "defender:pricings",
        "defender:secure_score",
        "defender:assessments",
    ],
    "Advisor": [
        "advisor:recommendations",
    ],
    "Monitoring": [
        "monitor:diag_coverage_sample",
    ],
    "Identity": [
        "identity:rbac_hygiene",
    ],
}


def probe_signal_availability(
    bus: SignalBus,
    scope: EvalScope,
) -> dict[str, Any]:
    """
    Probe each registered signal and return availability matrix.
    Returns {source: [{signal, status_icon, status, ms}]}.

    Uses bus.fetch_many() to probe all signals in parallel rather
    than sequentially, significantly reducing wall-clock time.
    """
    matrix: dict[str, list[dict]] = {}

    # Collect all registered signal names for a single parallel fetch
    all_registered: list[str] = []
    unregistered: dict[str, list[str]] = {}  # source -> [names]
    for source, signal_names in SIGNAL_SOURCES.items():
        unreg = []
        for name in signal_names:
            if name not in SIGNAL_PROVIDERS:
                unreg.append(name)
            else:
                all_registered.append(name)
        if unreg:
            unregistered[source] = unreg

    # Fetch all registered signals in parallel
    fetched = bus.fetch_many(all_registered, scope) if all_registered else {}

    # Build matrix from results
    for source, signal_names in SIGNAL_SOURCES.items():
        entries = []
        for name in signal_names:
            if name in unregistered.get(source, []):
                entries.append({"signal": name, "icon": "⊘", "status": "unregistered", "ms": 0})
                continue

            result = fetched.get(name)
            if result is None:
                entries.append({"signal": name, "icon": "❌", "status": "error: not fetched", "ms": 0})
                continue

            if result.status == SignalStatus.OK:
                icon = "✅"
                status = "OK"
            elif result.status == SignalStatus.NOT_AVAILABLE:
                icon = "⚠️"
                status = "unavailable"
            else:
                icon = "❌"
                status = f"error: {result.error_msg[:60]}" if result.error_msg else "error"

            entries.append({
                "signal": name,
                "icon": icon,
                "status": status,
                "ms": result.duration_ms,
            })
        matrix[source] = entries

    return matrix


def print_signal_matrix(matrix: dict[str, list[dict]]) -> None:
    """Pretty-print the signal availability matrix to terminal."""
    print("\n┌──────────────────────────────────────────────────────────────┐")
    print("│  Signal Availability Matrix                                  │")
    print("├──────────────────────────────────────────────────────────────┤")

    total_ok = 0
    total_signals = 0

    for source, entries in matrix.items():
        ok = sum(1 for e in entries if e["status"] == "OK")
        total = len(entries)
        total_ok += ok
        total_signals += total
        print(f"│  {source:<20} {ok}/{total} signals available")

        for e in entries:
            short_name = e["signal"].split(":")[-1]
            ms = f" ({e['ms']}ms)" if e["ms"] else ""
            print(f"│    {e['icon']}  {short_name:<35}{ms}")

    print("├──────────────────────────────────────────────────────────────┤")
    print(f"│  Total: {total_ok}/{total_signals} signals operational"
          f"{'':>{50 - len(str(total_ok)) - len(str(total_signals))}}")
    print("└──────────────────────────────────────────────────────────────┘")
