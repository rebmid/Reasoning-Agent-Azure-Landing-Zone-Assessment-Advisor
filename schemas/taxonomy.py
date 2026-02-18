# schemas/taxonomy.py — Single authoritative taxonomy for ALZ design areas.
"""Centralised taxonomy for Azure Landing Zone design areas.

Every classification mapping that was previously scattered across
``engine/adapter.py``, ``engine/scoring.py``, ``ai/build_advisor_payload.py``,
and ``reporting/render.py`` is defined here exactly once.

Phase-1 contract
~~~~~~~~~~~~~~~~
* Pure extraction — each dict is byte-for-byte identical to the original.
* Run JSON shape is unchanged.
* Scoring logic is unchanged.
* Telemetry layer is untouched.
* Fallback behaviour is unchanged.
"""
from __future__ import annotations

from typing import Literal, get_args


# ══════════════════════════════════════════════════════════════════
# Canonical enum  (for type-checking only — not used at runtime yet)
# ══════════════════════════════════════════════════════════════════

ALZDesignArea = Literal[
    "network_topology_and_connectivity",
    "identity_and_access_management",
    "governance",
    "management",
    "security",
    "data_protection",
    "resilience",
    "cost",
    "resource_organization",
    "platform_automation_and_devops",
    "azure_billing_and_entra_tenant",
]

ALL_DESIGN_AREAS: tuple[str, ...] = get_args(ALZDesignArea)


# ══════════════════════════════════════════════════════════════════
# Extracted from engine/adapter.py  (_DESIGN_AREA_SECTION)
# ══════════════════════════════════════════════════════════════════

DESIGN_AREA_SECTION: dict[str, str] = {
    "network": "Networking",
    "governance": "Governance",
    "security": "Security",
    "data_protection": "Data Protection",
    "resilience": "Resilience",
    "identity": "Identity",
}


# ══════════════════════════════════════════════════════════════════
# Extracted from engine/scoring.py  (DOMAIN_WEIGHTS)
# ══════════════════════════════════════════════════════════════════

DOMAIN_WEIGHTS: dict[str, float] = {
    "Security": 1.5,
    "Networking": 1.4,
    "Governance": 1.3,
    "Identity": 1.4,
    "Platform": 1.2,
    "Management": 1.1,
    "Data Protection": 1.3,
    "Resilience": 1.2,
}


# ══════════════════════════════════════════════════════════════════
# Extracted from ai/build_advisor_payload.py  (_SECTION_TO_DESIGN_AREA)
# ══════════════════════════════════════════════════════════════════

SECTION_TO_DESIGN_AREA: dict[str, str] = {
    "Security": "Security",
    "Networking": "Network Topology and Connectivity",
    "Network Coverage": "Network Topology and Connectivity",
    "Governance": "Governance",
    "Identity": "Identity and Access Management",
    "Management": "Management",
    "Data Protection": "Security",
    "Resilience": "Management",
    "Platform": "Platform Automation and DevOps",
    "Cost": "Governance",
}


# ══════════════════════════════════════════════════════════════════
# Extracted from reporting/render.py  (_DOMAIN_BUCKETS)
# ══════════════════════════════════════════════════════════════════

DOMAIN_BUCKETS: dict[str, list[str]] = {
    "Identity and Access Management": ["Identity and Access Management", "Azure Billing and Microsoft Entra ID Tenants", "Identity"],
    "Network Topology and Connectivity": ["Networking", "Network Topology and Connectivity"],
    "Governance": ["Governance", "Resource Organization"],
    "Security": ["Security"],
    "Management and Operations": ["Management", "Platform Automation and DevOps", "Operations"],
}


def bucket_domain(raw: str) -> str:
    """Map a raw section/category name to its report domain bucket.

    Extracted from ``reporting/render.py`` — logic is identical.
    """
    for bucket, members in DOMAIN_BUCKETS.items():
        if raw in members:
            return bucket
    return raw


# ══════════════════════════════════════════════════════════════════
# Extracted from reporting/render.py  (_MODE_SECTIONS)
# ══════════════════════════════════════════════════════════════════

MODE_SECTIONS: dict[str, list[str]] = {
    "Scale": ["Resource Organization", "Azure Billing and Microsoft Entra ID Tenants",
              "Identity and Access Management", "Governance"],
    "Security": ["Security", "Identity and Access Management"],
    "Operations": ["Management", "Platform Automation and DevOps"],
    "Cost": ["Governance", "Azure Billing and Microsoft Entra ID Tenants"],
    "Data Confidence": [],
}
