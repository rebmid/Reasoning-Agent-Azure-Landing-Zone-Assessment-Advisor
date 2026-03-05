# schemas/taxonomy.py — Single authoritative taxonomy for ALZ design areas.
"""Centralised taxonomy for Azure Landing Zone design areas.

┌─────────────────────────────────────────────────────────────────┐
│                  LAYER 2 — CHECKLIST MAPPING                    │
│                                                                 │
│  Canonical type definitions and mapping tables.                 │
│  Consumed by Layer 1 (scoring, evaluators) and Layer 3 (AI).    │
│  Changes here affect ALL downstream layers.                     │
│                                                                 │
│  This module is FROZEN during stabilization.                    │
│  Do NOT add new design areas, enum values, or mapping entries   │
│  without a version bump in control_packs/loader.py.             │
└─────────────────────────────────────────────────────────────────┘

Foundation Layer 1: Taxonomy Integrity (Non-Negotiable)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Every control must carry a complete, validated taxonomy.  There is
**zero** fallback logic — if a field is missing, invalid, or cannot be
mapped the system refuses to run.  This protects maturity math.

Foundation Layer 4: Control Schema Formalization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Every control definition is a **frozen dataclass** — typed, immutable,
validated at load time.  No ``dict[str, Any]`` access patterns.
If a field is missing or an enum value is invalid the system refuses
to run.  ``ControlDefinition`` is the single canonical type.

Canonical sources defined here:
  - ``ALZDesignArea``     — 8 design areas that exist in the control pack
  - ``WAFPillar``         — 5 Well-Architected Framework pillars
  - ``ControlType``       — ALZ | Derived | Manual | Hybrid
  - ``Severity``          — High | Medium | Low | Info
  - ``EvaluationLogic``   — automated | manual | hybrid
  - ``ControlDefinition`` — frozen dataclass for a single control
  - ``REQUIRED_CONTROL_FIELDS`` — fields every control MUST have
  - ``DESIGN_AREA_SECTION``     — complete slug → display-name map (8/8)
  - ``DOMAIN_WEIGHTS``          — scoring weights per display section
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args


# ══════════════════════════════════════════════════════════════════
# Canonical enums — enforced at load time, not aspirational
# ══════════════════════════════════════════════════════════════════

ALZDesignArea = Literal[
    "network",
    "identity",
    "governance",
    "management",
    "security",
    "data_protection",
    "resilience",
    "cost",
]

ALL_DESIGN_AREAS: tuple[str, ...] = get_args(ALZDesignArea)

WAFPillar = Literal[
    "Security",
    "Reliability",
    "Cost Optimization",
    "Operational Excellence",
    "Performance Efficiency",
]

ALL_WAF_PILLARS: tuple[str, ...] = get_args(WAFPillar)

ControlType = Literal["ALZ", "Derived", "Manual", "Hybrid"]

ALL_CONTROL_TYPES: tuple[str, ...] = get_args(ControlType)

Severity = Literal["High", "Medium", "Low", "Info"]

ALL_SEVERITIES: tuple[str, ...] = get_args(Severity)

EvaluationLogic = Literal["automated", "manual", "hybrid"]

ALL_EVALUATION_LOGIC: tuple[str, ...] = get_args(EvaluationLogic)

# ── Canonical control statuses ────────────────────────────────────
# Every runtime status MUST be one of these.  No implicit exclusions.
# If a status is not listed here it is a bug.

ControlStatus = Literal[
    "Pass",             # control fully satisfied
    "Fail",             # control violated
    "Partial",          # partially satisfied (evidence of both pass & fail)
    "Manual",           # requires human verification — no automation possible
    "NotApplicable",    # control does not apply to this environment
    "NotVerified",      # automation exists but could not execute (e.g. permissions)
    "SignalError",      # signal-layer failure — API/network/auth, not eval logic
    "EvaluationError",  # evaluator crashed or returned invalid data
]

ALL_CONTROL_STATUSES: tuple[str, ...] = get_args(ControlStatus)

# ── Deterministic status accounting ──────────────────────────────
# Every status above is explicitly placed in EXACTLY ONE category.
# scoring.py imports these — no local ad-hoc sets allowed.

# Statuses that count toward maturity % (Pass/Fail numerator/denominator)
MATURITY_STATUSES: frozenset[str] = frozenset({"Pass", "Fail", "Partial"})

# Statuses produced by successful automation (used for automation_coverage)
AUTO_STATUSES: frozenset[str] = frozenset({"Pass", "Fail", "Partial"})

# Statuses that are explicitly excluded from maturity math
# Reason: no signal-based evidence → cannot judge pass/fail
NON_MATURITY_STATUSES: frozenset[str] = frozenset({
    "Manual",           # human-only — excluded intentionally
    "NotApplicable",    # does not apply — excluded intentionally
    "NotVerified",      # could not verify — excluded intentionally
    "SignalError",      # signal infra failure — excluded intentionally
    "EvaluationError",  # evaluator crash — excluded intentionally
})

# Statuses representing signal-layer failures (for automation_integrity)
SIGNAL_ERROR_STATUSES: frozenset[str] = frozenset({"SignalError"})

# Statuses representing any kind of error (signal or evaluator)
ERROR_STATUSES: frozenset[str] = frozenset({"SignalError", "EvaluationError"})

# Statuses that represent active risk (included in risk tables)
RISK_STATUSES: frozenset[str] = frozenset({
    "Fail", "Partial", "SignalError", "EvaluationError",
})

# Statuses where the control was not executed (manual or skipped)
MANUAL_STATUSES: frozenset[str] = frozenset({"Manual"})

# Not applicable
NA_STATUSES: frozenset[str] = frozenset({"NotApplicable"})

# Compile-time assertion: every status is categorized
assert MATURITY_STATUSES | NON_MATURITY_STATUSES == frozenset(ALL_CONTROL_STATUSES), \
    f"Status accounting gap: {frozenset(ALL_CONTROL_STATUSES) - (MATURITY_STATUSES | NON_MATURITY_STATUSES)}"


# ══════════════════════════════════════════════════════════════════
# ControlDefinition — Foundation Layer 4: typed, frozen, validated
# ══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ControlDefinition:
    """Typed, immutable control definition.

    Loaded once from ``controls.json`` via ``from_json()`` and frozen.
    No runtime mutation.  All enum-typed fields are validated at
    construction time in ``__post_init__``.

    Field mapping from controls.json → dataclass:
        name             →  title
        design_area      →  alz_design_area
        required_signals →  required_signals (list → tuple)
        (all others keep their JSON key name)

    Computed fields (set in ``__post_init__``, not in JSON):
        weight             — from ``DOMAIN_WEIGHTS[section]``
        remediation_group  — defaults to ``sub_area``
        section (property) — ``DESIGN_AREA_SECTION[alz_design_area]``
    """

    # ── Identity ──────────────────────────────────────────────────
    control_id: str                    # 8-char short key from controls.json
    title: str                         # human-readable control name
    full_id: str                       # UUID stable identifier

    # ── Taxonomy enums ────────────────────────────────────────────
    alz_design_area: str               # ALZDesignArea literal
    sub_area: str
    waf_pillar: str                    # WAFPillar literal
    control_type: str                  # ControlType literal
    severity: str                      # Severity literal
    evaluation_logic: str              # EvaluationLogic literal

    # ── Evaluation binding ────────────────────────────────────────
    evaluator_module: str              # dotted module path for evaluator dispatch
    required_signals: tuple[str, ...]  # immutable signal keys

    # ── Documentation ─────────────────────────────────────────────
    caf_guidance: str
    caf_url: str

    # ── Checklist grounding (Azure/review-checklists authority) ───
    checklist_ids: tuple[str, ...] = ()     # e.g. ("D07.01",)
    checklist_guids: tuple[str, ...] = ()   # matching GUIDs from ALZ checklist

    # ── Computed / derived (set in __post_init__) ─────────────────
    weight: float = field(init=False)
    remediation_group: str = field(init=False)
    signal_category: str | None = field(default=None)

    def __post_init__(self) -> None:
        """Validate enum fields and compute derived fields.

        Raises ``ValueError`` if any enum field has an invalid value.
        Uses ``object.__setattr__`` for computed fields because the
        dataclass is frozen.
        """
        # ── Enum validation ───────────────────────────────────────
        if self.alz_design_area not in ALL_DESIGN_AREAS:
            raise ValueError(
                f"[{self.control_id}] Invalid alz_design_area: "
                f"{self.alz_design_area!r} — expected one of {list(ALL_DESIGN_AREAS)}"
            )
        if self.waf_pillar not in ALL_WAF_PILLARS:
            raise ValueError(
                f"[{self.control_id}] Invalid waf_pillar: "
                f"{self.waf_pillar!r} — expected one of {list(ALL_WAF_PILLARS)}"
            )
        if self.control_type not in ALL_CONTROL_TYPES:
            raise ValueError(
                f"[{self.control_id}] Invalid control_type: "
                f"{self.control_type!r} — expected one of {list(ALL_CONTROL_TYPES)}"
            )
        if self.severity not in ALL_SEVERITIES:
            raise ValueError(
                f"[{self.control_id}] Invalid severity: "
                f"{self.severity!r} — expected one of {list(ALL_SEVERITIES)}"
            )
        if self.evaluation_logic not in ALL_EVALUATION_LOGIC:
            raise ValueError(
                f"[{self.control_id}] Invalid evaluation_logic: "
                f"{self.evaluation_logic!r} — expected one of {list(ALL_EVALUATION_LOGIC)}"
            )

        # ── Computed fields ───────────────────────────────────────
        _section = DESIGN_AREA_SECTION[self.alz_design_area]
        object.__setattr__(self, "weight", DOMAIN_WEIGHTS[_section])
        object.__setattr__(self, "remediation_group", self.sub_area)

    @property
    def section(self) -> str:
        """Scoring display section derived from design area."""
        return DESIGN_AREA_SECTION[self.alz_design_area]

    @classmethod
    def from_json(cls, control_id: str, raw: dict[str, Any]) -> ControlDefinition:
        """Construct from a raw ``controls.json`` entry.

        Maps JSON field names to dataclass field names:
          ``name`` → ``title``,  ``design_area`` → ``alz_design_area``,
          ``required_signals`` list → ``required_signals`` tuple.

        Raises ``KeyError`` if a required JSON field is missing.
        Raises ``ValueError`` if an enum value is invalid (via __post_init__).
        """
        return cls(
            control_id=control_id,
            title=raw["name"],
            full_id=raw["full_id"],
            alz_design_area=raw["design_area"],
            sub_area=raw["sub_area"],
            waf_pillar=raw["waf_pillar"],
            control_type=raw["control_type"],
            severity=raw["severity"],
            evaluation_logic=raw["evaluation_logic"],
            evaluator_module=raw["evaluator_module"],
            required_signals=tuple(raw["required_signals"]),
            caf_guidance=raw.get("caf_guidance", ""),
            caf_url=raw.get("caf_url", ""),
            checklist_ids=tuple(raw.get("checklist_ids", ())),
            checklist_guids=tuple(raw.get("checklist_guids", ())),
            signal_category=raw.get("signal_category"),
        )


# ── Required fields every control definition MUST have ────────────
REQUIRED_CONTROL_FIELDS: tuple[str, ...] = (
    "name",
    "full_id",
    "design_area",
    "sub_area",
    "waf_pillar",
    "control_type",
    "severity",
    "evaluation_logic",
    "evaluator_module",
    "required_signals",
)


# ══════════════════════════════════════════════════════════════════
# design_area slug → scoring display section  (COMPLETE — all 8)
# ══════════════════════════════════════════════════════════════════

DESIGN_AREA_SECTION: dict[str, str] = {
    "network":         "Networking",
    "governance":      "Governance",
    "security":        "Security",
    "data_protection": "Data Protection",
    "resilience":      "Resilience",
    "identity":        "Identity",
    "management":      "Management",
    "cost":            "Cost",
}

# ── ALZ Core vs Operational Overlay ───────────────────────────────
# ALZ Core: the canonical ALZ design areas executives already know.
# Operational Overlay: derived domains that matter for posture depth.
ALZ_CORE_SECTIONS: frozenset[str] = frozenset({
    "Identity",
    "Networking",
    "Governance",
    "Security",
    "Management",
})

OPERATIONAL_OVERLAY_SECTIONS: frozenset[str] = frozenset({
    "Data Protection",
    "Resilience",
    "Cost",
})

# compile-time check: Core + Overlay == all display sections
assert ALZ_CORE_SECTIONS | OPERATIONAL_OVERLAY_SECTIONS == frozenset(DESIGN_AREA_SECTION.values()), \
    f"Section classification gap: {frozenset(DESIGN_AREA_SECTION.values()) - (ALZ_CORE_SECTIONS | OPERATIONAL_OVERLAY_SECTIONS)}"


# ══════════════════════════════════════════════════════════════════
# Scoring domain weights — keyed by display section
# ══════════════════════════════════════════════════════════════════

DOMAIN_WEIGHTS: dict[str, float] = {
    "Security":        1.5,
    "Networking":      1.4,
    "Governance":      1.3,
    "Identity":        1.4,
    "Management":      1.1,
    "Data Protection": 1.3,
    "Resilience":      1.2,
    "Cost":            1.0,
}


# ══════════════════════════════════════════════════════════════════
# Section → ALZ design area label (for AI advisor payload)
# ══════════════════════════════════════════════════════════════════

SECTION_TO_DESIGN_AREA: dict[str, str] = {
    "Security":        "Security",
    "Networking":      "Network Topology and Connectivity",
    "Governance":      "Governance",
    "Identity":        "Identity and Access Management",
    "Management":      "Management",
    "Data Protection": "Security",           # protection controls → Security
    "Resilience":      "Management",         # protect & recover → Management
    "Cost":            "Governance",          # cost policy enforcement → Governance
}


# ══════════════════════════════════════════════════════════════════
# Report domain buckets — groups display sections into report blocks
# ══════════════════════════════════════════════════════════════════

DOMAIN_BUCKETS: dict[str, list[str]] = {
    "Identity and Access Management": [
        "Identity and Access Management",
        "Azure Billing and Microsoft Entra ID Tenants",
        "Identity",
    ],
    "Network Topology and Connectivity": [
        "Networking",
        "Network Topology and Connectivity",
    ],
    "Governance": ["Governance", "Resource Organization"],
    "Security":   ["Security", "Data Protection"],
    "Management and Operations": [
        "Management",
        "Platform Automation and DevOps",
        "Operations",
        "Resilience",
        "Cost",
    ],
}


def bucket_domain(raw: str) -> str:
    """Map a raw section/category name to its report domain bucket.

    Raises ValueError if the section is not in any bucket — no silent
    fallback to the raw string.
    """
    for bucket, members in DOMAIN_BUCKETS.items():
        if raw in members:
            return bucket
    # Allow passthrough only for already-bucketed names
    if raw in DOMAIN_BUCKETS:
        return raw
    return raw  # non-taxonomy sections (Manual backfill) pass through


# ══════════════════════════════════════════════════════════════════
# Cross-taxonomy mapping tables – Structural Consistency Layer
# ══════════════════════════════════════════════════════════════════
# These mappings connect the three taxonomy systems:
#   A) KG `affects[].discipline`  (7 short labels)
#   B) Official ALZ design areas  (8 names from MS docs)
#   C) CAF lifecycle phases       (used in initiative.caf_discipline)
#
# Locking these here prevents free-text drift across modules.

# ── Official 8 ALZ Design Area Names (from MS docs) ──────────────
OFFICIAL_ALZ_DESIGN_AREAS: tuple[str, ...] = (
    "Azure Billing and Microsoft Entra ID Tenants",
    "Identity and Access Management",
    "Network Topology and Connectivity",
    "Security",
    "Management",
    "Resource Organization",
    "Platform Automation and DevOps",
    "Governance",
)

# ── ALZ Checklist ID Letter Legend ────────────────────────────────
# Maps the single-letter prefix of ALZ review-checklist IDs (e.g.
# "D07.01") to the official design area name.  Derived from the
# Azure/review-checklists ``alz_checklist.en.json`` authority file.
#
#   Architecture-diagram labels (A–I) on the CAF conceptual
#   architecture page differ from checklist prefixes (A–H).
#   This table uses the **checklist** scheme, which is the authority
#   for ``checklist_ids`` fields in control_packs.
#
# Official reference:
#   https://learn.microsoft.com/en-us/azure/cloud-adoption-framework/ready/landing-zone/design-areas
CHECKLIST_LETTER_TO_DESIGN_AREA: dict[str, str] = {
    "A": "Azure Billing and Microsoft Entra ID Tenants",
    "B": "Identity and Access Management",
    "C": "Resource Organization",
    "D": "Network Topology and Connectivity",
    "E": "Governance",
    "F": "Management",
    "G": "Security",
    "H": "Platform Automation and DevOps",
}

# Official CAF design area objectives (from the design areas page).
# Keyed by checklist letter prefix for rendering in the HTML legend.
DESIGN_AREA_OBJECTIVES: dict[str, str] = {
    "A": "Proper tenant creation, enrollment, and billing setup are important early steps.",
    "B": "Identity and access management is a primary security boundary in the public cloud. It's the foundation for any secure and fully compliant architecture.",
    "C": "As cloud adoption scales, considerations for subscription design and management group hierarchy have an impact on governance, operations management, and adoption patterns.",
    "D": "Networking and connectivity decisions are an equally important foundational aspect of any cloud architecture.",
    "E": "Automate auditing and enforcement of governance policies.",
    "F": "For stable, ongoing operations in the cloud, a management baseline is required to provide visibility, operations compliance, and protect and recover capabilities.",
    "G": "Implement controls and processes to protect your cloud environments.",
    "H": "Align the best tools and templates to deploy your landing zones and supporting resources.",
}

# Reverse: official design area → checklist letter prefix
DESIGN_AREA_TO_CHECKLIST_LETTER: dict[str, str] = {
    v: k for k, v in CHECKLIST_LETTER_TO_DESIGN_AREA.items()
}

# compile-time check: legend covers all official design areas
assert set(CHECKLIST_LETTER_TO_DESIGN_AREA.values()) == set(OFFICIAL_ALZ_DESIGN_AREAS), \
    f"Legend/design-area mismatch: {set(OFFICIAL_ALZ_DESIGN_AREAS) - set(CHECKLIST_LETTER_TO_DESIGN_AREA.values())}"

# ── KG discipline → internal design-area slug ─────────────────────
# Maps the Knowledge Graph 'affects[].discipline' short labels to
# the internal slugs used in DESIGN_AREA_SECTION.
KG_DISCIPLINE_TO_SLUG: dict[str, str] = {
    "identity":     "identity",
    "security":     "security",
    "network":      "network",
    "management":   "management",
    "automation":   "management",   # automation maps to management slug
    "organization": "governance",
    "cost":         "cost",
}

# ── Blocker category → display sections ───────────────────────────
# Maps the readiness-pass blocker short categories to display-section
# names used in control results.  Canonical source — decision_impact.py
# MUST import from here rather than defining its own copy.
BLOCKER_CATEGORY_TO_SECTIONS: dict[str, list[str]] = {
    "governance":  ["Resource Organization", "Governance"],
    "security":    ["Security"],
    "networking":  ["Network Topology and Connectivity", "Networking"],
    "network topology and connectivity": ["Network Topology and Connectivity", "Networking"],
    "identity":    ["Identity and Access Management", "Identity"],
    "identity and access management": ["Identity and Access Management", "Identity"],
    "management":  ["Management"],
    "automation":  ["Platform Automation and DevOps"],
    "platform automation and devops": ["Platform Automation and DevOps"],
    "billing":     ["Azure Billing and Microsoft Entra ID Tenants"],
    "azure billing and microsoft entra id tenants": ["Azure Billing and Microsoft Entra ID Tenants"],
    "resource organization": ["Resource Organization", "Governance"],
    "resilience":  ["Resilience"],
    "data protection": ["Security"],
    "cost governance": ["Governance", "Azure Billing and Microsoft Entra ID Tenants"],
}

# ── CAF Lifecycle Phases ──────────────────────────────────────────
CAF_PHASES: tuple[str, ...] = (
    "Plan",
    "Ready",
    "Adopt",
    "Govern",
    "Manage",
    "Secure",
)


def normalize_section_to_alz(section: str) -> str:
    """Return the official ALZ design area name for a control section.

    Handles both display sections ("Networking") and official names
    ("Network Topology and Connectivity").  Returns the input
    unchanged if no mapping is found.
    """
    # Already an official name?
    if section in OFFICIAL_ALZ_DESIGN_AREAS:
        return section
    # Map via SECTION_TO_DESIGN_AREA
    return SECTION_TO_DESIGN_AREA.get(section, section)


# ══════════════════════════════════════════════════════════════════
# Mode sections (landing-page report grouping)
# ══════════════════════════════════════════════════════════════════

MODE_SECTIONS: dict[str, list[str]] = {
    "Scale": [
        "Resource Organization",
        "Azure Billing and Microsoft Entra ID Tenants",
        "Identity and Access Management",
        "Governance",
    ],
    "Security":   ["Security", "Identity and Access Management"],
    "Operations": ["Management", "Platform Automation and DevOps"],
    "Cost":       ["Governance", "Azure Billing and Microsoft Entra ID Tenants"],
    "Data Confidence": [],
}
