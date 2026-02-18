# Control Definition Schema & Scalable Results Data Model

> **lz-assessor** architecture specification  
> Author: GitHub Copilot — February 17, 2026  
> Status: Proposed — pending implementation

---

## Table of Contents

1. [Control Definition Schema](#control-definition-schema)
   - [Problem Statement](#problem-statement)
   - [Design Principles](#design-principles)
   - [Canonical Enums](#canonical-enums)
   - [JSON Schema](#json-schema)
   - [Example Control Definitions](#example-control-definitions)
   - [Mapping Rules](#mapping-rules)
   - [Validation Strategy](#validation-strategy)
2. [Scalable Results Data Model](#scalable-results-data-model)
   - [Model Choice](#model-choice)
   - [Entity Definitions](#entity-definitions)
   - [Aggregation Logic](#aggregation-logic)
   - [Provenance & Telemetry](#provenance--telemetry)
   - [3-Subscription Example](#3-subscription-example)
   - [Executive Rollup Example](#executive-rollup-example)

---

# Control Definition Schema

## Problem Statement

The current codebase has **three parallel taxonomy systems** with no shared key:

| System | Field | Vocabulary | File |
|---|---|---|---|
| Knowledge graph | `discipline` | 7 CAF keys (`identity`, `security`, `network`, ...) | `graph/controls.json` |
| Control pack | `design_area` | 8 lowercase slugs (`network`, `governance`, ...) | `control_packs/alz/v1.0/controls.json` |
| Scoring/reporting | `section` | Display names (`"Networking"`, `"Governance"`, ...) | `engine/adapter.py` → everywhere |

These are bridged by **four different hardcoded mapping dicts** that don't agree:

- `engine/adapter.py` → `_DESIGN_AREA_SECTION` (6 entries, missing `management` and `cost`)
- `ai/build_advisor_payload.py` → `_SECTION_TO_DESIGN_AREA` (10 entries, lossy round-trip)
- `reporting/render.py` → `_DOMAIN_BUCKETS` (5 buckets, mixes checklist + adapter vocabulary)
- `reporting/render.py` → `_MODE_SECTIONS` (5 modes, uses ALZ checklist names)

Additionally:
- `category` and `section` are always identical in adapted results (redundant)
- `text` and `question` are always identical (redundant)
- Manual checklist items use ALZ checklist `category` (e.g., "Network Topology and Connectivity") while automated controls use adapter `section` (e.g., "Networking") — they score into different buckets
- WAF pillar only exists if an ALZ checklist GUID matches — never flows through automated path
- `DOMAIN_WEIGHTS` in `scoring.py` must be manually kept in sync with adapter output

### Goal

**One authoritative taxonomy field — `alz_design_area` — at the control definition layer. Everything else derives from it.**

## Design Principles

1. **Single source of truth.** `alz_design_area` is an enum. Period.
2. **Derived, not competing.** Platform domains, WAF pillars, scoring sections, and initiative groupings are all **computed lookups** from `alz_design_area` (or from control metadata). Never free-text.
3. **Stable IDs.** Control IDs follow a hierarchical pattern: `{area_prefix}{sequence}.{sub}` (e.g., `N01.02`, `S03.14`). Once assigned, never change.
4. **Dual evaluation modes.** Every control declares `evaluation_mode: "data_driven" | "questionnaire" | "hybrid"`. Data-driven controls require `evidence_sources[]`. Questionnaire controls require `question_template`.
5. **Schema-enforced.** JSON Schema validation prevents bad taxonomy from entering the system.

## Canonical Enums

### `alz_design_area` — THE authoritative enum (11 values)

```
network_topology_and_connectivity
identity_and_access_management
governance
management
security
data_protection
resilience
cost
resource_organization
platform_automation_and_devops
azure_billing_and_entra_tenant
```

### Derived: `platform_domain` (computed from `alz_design_area`)

```python
ALZ_TO_PLATFORM_DOMAIN = {
    "network_topology_and_connectivity":    "Networking",
    "identity_and_access_management":       "Identity",
    "governance":                           "Governance",
    "management":                           "Management",
    "security":                             "Security",
    "data_protection":                      "Security",       # rolls up
    "resilience":                           "Reliability",    # WAF-aligned
    "cost":                                 "Cost",
    "resource_organization":                "Governance",     # rolls up
    "platform_automation_and_devops":       "Platform",
    "azure_billing_and_entra_tenant":       "Governance",     # rolls up
}
```

### Derived: `waf_pillar` (computed from `alz_design_area`)

```python
ALZ_TO_WAF_PILLAR = {
    "network_topology_and_connectivity":    "Reliability",
    "identity_and_access_management":       "Security",
    "governance":                           "Operational Excellence",
    "management":                           "Operational Excellence",
    "security":                             "Security",
    "data_protection":                      "Security",
    "resilience":                           "Reliability",
    "cost":                                 "Cost Optimization",
    "resource_organization":                "Operational Excellence",
    "platform_automation_and_devops":       "Operational Excellence",
    "azure_billing_and_entra_tenant":       "Cost Optimization",
}
```

> **Note:** Some controls may map to secondary WAF pillars (e.g., a network control that also affects Security). The schema supports `waf_pillar_overrides[]` for this.

### Derived: `initiative_group` (for remediation program clustering)

```python
ALZ_TO_INITIATIVE_GROUP = {
    "network_topology_and_connectivity":    "Network Architecture",
    "identity_and_access_management":       "Identity & Zero Trust",
    "governance":                           "Governance & Compliance",
    "management":                           "Observability & Operations",
    "security":                             "Security Posture",
    "data_protection":                      "Data Security",
    "resilience":                           "Business Continuity",
    "cost":                                 "FinOps & Optimization",
    "resource_organization":                "Governance & Compliance",
    "platform_automation_and_devops":       "Platform Engineering",
    "azure_billing_and_entra_tenant":       "FinOps & Optimization",
}
```

### Derived: `scoring_section` (for DOMAIN_WEIGHTS)

```python
ALZ_TO_SCORING_SECTION = {
    "network_topology_and_connectivity":    "Networking",
    "identity_and_access_management":       "Identity",
    "governance":                           "Governance",
    "management":                           "Management",
    "security":                             "Security",
    "data_protection":                      "Data Protection",
    "resilience":                           "Resilience",
    "cost":                                 "Cost",
    "resource_organization":                "Governance",
    "platform_automation_and_devops":       "Platform",
    "azure_billing_and_entra_tenant":       "Governance",
}
```

All four mapping dicts live in **one file** (`schemas/taxonomy.py`), imported by adapter, scoring, AI payload builder, and reporting. No more scattered hardcoded mappings.

## JSON Schema

```jsonc
// schemas/control_definition.schema.json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://lz-assessor/schemas/control_definition.schema.json",
  "title": "Control Definition",
  "description": "Authoritative control definition for lz-assessor. alz_design_area is the single taxonomy key.",
  "type": "object",
  "required": [
    "control_id",
    "name",
    "alz_design_area",
    "severity",
    "evaluation_mode",
    "version"
  ],
  "properties": {
    "control_id": {
      "type": "string",
      "description": "Stable hierarchical ID. Pattern: {area_prefix}{sequence}.{sub}",
      "pattern": "^[A-Z]{1,3}[0-9]{2}\\.[0-9]{2}$",
      "examples": ["N01.02", "S03.14", "G05.01"]
    },
    "legacy_id": {
      "type": "string",
      "description": "Previous 8-char short ID or GUID for migration linkage"
    },
    "name": {
      "type": "string",
      "description": "Human-readable control name",
      "minLength": 5,
      "maxLength": 200
    },
    "description": {
      "type": "string",
      "description": "Detailed control description for reports and AI context"
    },
    "alz_design_area": {
      "type": "string",
      "description": "THE authoritative ALZ taxonomy field",
      "enum": [
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
        "azure_billing_and_entra_tenant"
      ]
    },
    "alz_sub_area": {
      "type": "string",
      "description": "Optional refinement within the design area (e.g., 'Hub-Spoke Topology', 'PIM Configuration'). Controlled vocabulary per design_area.",
      "maxLength": 100
    },
    "waf_pillar_overrides": {
      "type": "array",
      "description": "Secondary WAF pillars beyond the default derived from alz_design_area. Empty = use default only.",
      "items": {
        "type": "string",
        "enum": ["Reliability", "Security", "Cost Optimization", "Operational Excellence", "Performance Efficiency"]
      },
      "maxItems": 4
    },
    "severity": {
      "type": "string",
      "enum": ["Critical", "High", "Medium", "Low"],
      "description": "Risk severity for scoring weight"
    },
    "evaluation_mode": {
      "type": "string",
      "enum": ["data_driven", "questionnaire", "hybrid"],
      "description": "How this control is evaluated: automated signal analysis, manual questionnaire, or both"
    },
    "evidence_sources": {
      "type": "array",
      "description": "Required for data_driven and hybrid controls. Ordered list of signal sources the evaluator consumes.",
      "items": {
        "$ref": "#/$defs/evidence_source"
      },
      "minItems": 0
    },
    "question_template": {
      "type": "object",
      "description": "Required for questionnaire and hybrid controls",
      "properties": {
        "text": {
          "type": "string",
          "description": "Question presented to the assessor"
        },
        "type": {
          "type": "string",
          "enum": ["yes_no", "maturity_scale", "multi_select", "free_text"]
        },
        "scoring_map": {
          "type": "object",
          "description": "Maps answer values to status outcomes",
          "additionalProperties": {
            "type": "string",
            "enum": ["Pass", "Fail", "Partial"]
          }
        }
      },
      "required": ["text", "type"]
    },
    "evaluator_module": {
      "type": "string",
      "description": "Python module path for the evaluator function (e.g., 'evaluators.networking')"
    },
    "depends_on": {
      "type": "array",
      "description": "Control IDs that must pass before this control can be evaluated",
      "items": { "type": "string" }
    },
    "defer_if_parent_fails": {
      "type": "boolean",
      "default": false,
      "description": "If true, skip evaluation when any depends_on control fails"
    },
    "rationale_template": {
      "type": "object",
      "description": "Templates for generating human-readable rationale text",
      "properties": {
        "pass": { "type": "string", "description": "Template when status=Pass. Supports {evidence_count}, {coverage_pct} placeholders." },
        "fail": { "type": "string", "description": "Template when status=Fail" },
        "partial": { "type": "string", "description": "Template when status=Partial" },
        "manual": { "type": "string", "description": "Template when status=Manual" }
      }
    },
    "caf_guidance": {
      "type": "string",
      "description": "Short CAF/ALZ guidance text"
    },
    "caf_url": {
      "type": "string",
      "format": "uri",
      "description": "Link to official CAF/ALZ documentation"
    },
    "version": {
      "type": "string",
      "pattern": "^[0-9]+\\.[0-9]+$",
      "description": "Control definition version (e.g., '1.0'). Incremented when logic changes."
    },
    "tags": {
      "type": "array",
      "description": "Free-form tags for filtering (e.g., ['defender', 'baseline', 'zero-trust'])",
      "items": { "type": "string" }
    }
  },
  "$defs": {
    "evidence_source": {
      "type": "object",
      "required": ["signal_bus_name", "signal_category"],
      "properties": {
        "signal_bus_name": {
          "type": "string",
          "description": "Signal bus key (e.g., 'resource_graph:azure_firewall', 'defender:pricings')"
        },
        "signal_category": {
          "type": "string",
          "enum": [
            "resource_graph",
            "arm",
            "policy",
            "defender",
            "monitor",
            "identity",
            "cost",
            "network",
            "manage"
          ],
          "description": "Provider category for telemetry classification"
        },
        "description": {
          "type": "string",
          "description": "What this signal provides for this control"
        }
      }
    }
  },
  "allOf": [
    {
      "if": {
        "properties": { "evaluation_mode": { "const": "data_driven" } }
      },
      "then": {
        "required": ["evidence_sources", "evaluator_module"],
        "properties": {
          "evidence_sources": { "minItems": 1 }
        }
      }
    },
    {
      "if": {
        "properties": { "evaluation_mode": { "const": "questionnaire" } }
      },
      "then": {
        "required": ["question_template"]
      }
    },
    {
      "if": {
        "properties": { "evaluation_mode": { "const": "hybrid" } }
      },
      "then": {
        "required": ["evidence_sources", "question_template", "evaluator_module"],
        "properties": {
          "evidence_sources": { "minItems": 1 }
        }
      }
    }
  ]
}
```

## Example Control Definitions

### Example 1: Data-driven control (automated)

```json
{
  "control_id": "N01.02",
  "legacy_id": "e6c4cfd3",
  "name": "Azure Firewall deployed in hub VNet",
  "description": "Validates that Azure Firewall (or a supported NVA) is deployed in the hub virtual network for centralized traffic inspection.",
  "alz_design_area": "network_topology_and_connectivity",
  "alz_sub_area": "Hub-Spoke Topology",
  "severity": "High",
  "evaluation_mode": "data_driven",
  "evidence_sources": [
    {
      "signal_bus_name": "resource_graph:azure_firewall",
      "signal_category": "resource_graph",
      "description": "Queries Azure Firewall instances across all subscriptions"
    }
  ],
  "evaluator_module": "evaluators.networking",
  "depends_on": [],
  "defer_if_parent_fails": false,
  "rationale_template": {
    "pass": "Azure Firewall detected in {evidence_count} hub VNet(s). Centralized inspection is in place.",
    "fail": "No Azure Firewall found. Hub-spoke architecture lacks centralized traffic inspection.",
    "partial": "Azure Firewall found but covers only {coverage_pct}% of expected hub VNets."
  },
  "caf_guidance": "Use Azure Firewall or partner NVA in the hub for centralized network security.",
  "caf_url": "https://learn.microsoft.com/azure/cloud-adoption-framework/ready/azure-best-practices/hub-spoke-network-topology",
  "version": "1.0",
  "tags": ["networking", "hub-spoke", "firewall", "baseline"]
}
```

### Example 2: Questionnaire control (manual)

```json
{
  "control_id": "B03.01",
  "name": "EA/MCA billing agreement in place",
  "description": "Confirms that the organization has an Enterprise Agreement or Microsoft Customer Agreement with appropriate billing structure for landing zone deployment.",
  "alz_design_area": "azure_billing_and_entra_tenant",
  "severity": "Medium",
  "evaluation_mode": "questionnaire",
  "question_template": {
    "text": "Does the organization have an EA or MCA billing agreement with Azure?",
    "type": "yes_no",
    "scoring_map": {
      "yes": "Pass",
      "no": "Fail"
    }
  },
  "rationale_template": {
    "pass": "EA/MCA billing structure confirmed. Subscription vending can proceed.",
    "fail": "No enterprise billing agreement. Landing zone subscription provisioning may be blocked.",
    "manual": "Billing agreement status requires manual verification with finance/procurement team."
  },
  "caf_guidance": "Establish an EA or MCA before deploying landing zones at scale.",
  "caf_url": "https://learn.microsoft.com/azure/cloud-adoption-framework/ready/landing-zone/design-area/azure-billing-ad-tenant",
  "version": "1.0",
  "tags": ["billing", "prerequisites"]
}
```

### Example 3: Hybrid control (data + questionnaire)

```json
{
  "control_id": "S03.14",
  "legacy_id": "09945bda",
  "name": "Defender for Cloud enabled across workloads",
  "description": "Validates Microsoft Defender for Cloud is enabled with appropriate plan coverage across subscriptions. Signal data shows plan status; questionnaire captures intended coverage scope.",
  "alz_design_area": "security",
  "alz_sub_area": "Threat Protection",
  "waf_pillar_overrides": ["Reliability"],
  "severity": "Critical",
  "evaluation_mode": "hybrid",
  "evidence_sources": [
    {
      "signal_bus_name": "defender:pricings",
      "signal_category": "defender",
      "description": "Defender plan enablement status per subscription"
    },
    {
      "signal_bus_name": "defender:secure_score",
      "signal_category": "defender",
      "description": "Secure Score for posture measurement"
    }
  ],
  "question_template": {
    "text": "Which Defender plans are intended to be enabled organization-wide?",
    "type": "multi_select",
    "scoring_map": {
      "all_plans": "Pass",
      "some_plans": "Partial",
      "none": "Fail"
    }
  },
  "evaluator_module": "evaluators.security",
  "depends_on": [],
  "defer_if_parent_fails": false,
  "rationale_template": {
    "pass": "Defender enabled across {evidence_count} plan(s) with {coverage_pct}% subscription coverage.",
    "fail": "Defender is not enabled. {evidence_count} subscriptions have no Defender plan active.",
    "partial": "Defender partially enabled — {coverage_pct}% coverage. Some plans or subscriptions lack protection."
  },
  "caf_guidance": "Enable Defender for Cloud plans aligned to workload types deployed in each subscription.",
  "caf_url": "https://learn.microsoft.com/azure/cloud-adoption-framework/ready/landing-zone/design-area/security",
  "version": "1.0",
  "tags": ["defender", "security-posture", "zero-trust", "baseline"]
}
```

## Mapping Rules

### Rule 1: `platform_domain` — derived, never stored

```python
# schemas/taxonomy.py — THE SINGLE SOURCE OF TRUTH

from typing import Literal

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

# ── All mappings derive from alz_design_area ─────────────────────

ALZ_DESIGN_AREA_LABELS: dict[ALZDesignArea, str] = {
    "network_topology_and_connectivity":    "Network Topology & Connectivity",
    "identity_and_access_management":       "Identity & Access Management",
    "governance":                           "Governance",
    "management":                           "Management",
    "security":                             "Security",
    "data_protection":                      "Data Protection",
    "resilience":                           "Resilience",
    "cost":                                 "Cost",
    "resource_organization":                "Resource Organization",
    "platform_automation_and_devops":       "Platform Automation & DevOps",
    "azure_billing_and_entra_tenant":       "Azure Billing & Entra Tenant",
}

ALZ_TO_PLATFORM_DOMAIN: dict[ALZDesignArea, str] = {
    "network_topology_and_connectivity":    "Networking",
    "identity_and_access_management":       "Identity",
    "governance":                           "Governance",
    "management":                           "Management",
    "security":                             "Security",
    "data_protection":                      "Security",
    "resilience":                           "Reliability",
    "cost":                                 "Cost",
    "resource_organization":                "Governance",
    "platform_automation_and_devops":       "Platform",
    "azure_billing_and_entra_tenant":       "Governance",
}

ALZ_TO_WAF_PILLAR: dict[ALZDesignArea, str] = {
    "network_topology_and_connectivity":    "Reliability",
    "identity_and_access_management":       "Security",
    "governance":                           "Operational Excellence",
    "management":                           "Operational Excellence",
    "security":                             "Security",
    "data_protection":                      "Security",
    "resilience":                           "Reliability",
    "cost":                                 "Cost Optimization",
    "resource_organization":                "Operational Excellence",
    "platform_automation_and_devops":       "Operational Excellence",
    "azure_billing_and_entra_tenant":       "Cost Optimization",
}

ALZ_TO_SCORING_SECTION: dict[ALZDesignArea, str] = {
    "network_topology_and_connectivity":    "Networking",
    "identity_and_access_management":       "Identity",
    "governance":                           "Governance",
    "management":                           "Management",
    "security":                             "Security",
    "data_protection":                      "Data Protection",
    "resilience":                           "Resilience",
    "cost":                                 "Cost",
    "resource_organization":                "Governance",
    "platform_automation_and_devops":       "Platform",
    "azure_billing_and_entra_tenant":       "Governance",
}

ALZ_TO_INITIATIVE_GROUP: dict[ALZDesignArea, str] = {
    "network_topology_and_connectivity":    "Network Architecture",
    "identity_and_access_management":       "Identity & Zero Trust",
    "governance":                           "Governance & Compliance",
    "management":                           "Observability & Operations",
    "security":                             "Security Posture",
    "data_protection":                      "Data Security",
    "resilience":                           "Business Continuity",
    "cost":                                 "FinOps & Optimization",
    "resource_organization":                "Governance & Compliance",
    "platform_automation_and_devops":       "Platform Engineering",
    "azure_billing_and_entra_tenant":       "FinOps & Optimization",
}

# ── Scoring domain weights (keyed by scoring_section) ────────────
DOMAIN_WEIGHTS: dict[str, float] = {
    "Security":         1.5,
    "Networking":       1.4,
    "Identity":         1.4,
    "Governance":       1.3,
    "Data Protection":  1.3,
    "Platform":         1.2,
    "Resilience":       1.2,
    "Management":       1.1,
    "Cost":             1.0,
}


def platform_domain(area: ALZDesignArea) -> str:
    return ALZ_TO_PLATFORM_DOMAIN[area]

def waf_pillar(area: ALZDesignArea, overrides: list[str] | None = None) -> list[str]:
    primary = ALZ_TO_WAF_PILLAR[area]
    return [primary] + (overrides or [])

def scoring_section(area: ALZDesignArea) -> str:
    return ALZ_TO_SCORING_SECTION[area]

def initiative_group(area: ALZDesignArea) -> str:
    return ALZ_TO_INITIATIVE_GROUP[area]

def design_area_label(area: ALZDesignArea) -> str:
    return ALZ_DESIGN_AREA_LABELS[area]

def domain_weight(area: ALZDesignArea) -> float:
    section = ALZ_TO_SCORING_SECTION[area]
    return DOMAIN_WEIGHTS.get(section, 1.0)
```

### Rule 2: How the adapter changes

```python
# BEFORE (current — engine/adapter.py):
_DESIGN_AREA_SECTION = {"network": "Networking", ...}  # 6 entries, incomplete
section = _DESIGN_AREA_SECTION.get(area) or area.title()  # fragile fallback

# AFTER:
from schemas.taxonomy import scoring_section, platform_domain, waf_pillar, design_area_label

def adapt_evaluator_result(eval_result, control_def):
    area = control_def["alz_design_area"]
    return {
        "control_id":       control_def["control_id"],
        "alz_design_area":  area,                          # carry through!
        "section":          scoring_section(area),          # derived
        "platform_domain":  platform_domain(area),          # derived
        "waf_pillar":       waf_pillar(area, control_def.get("waf_pillar_overrides")),
        "design_area_label": design_area_label(area),       # for Excel col B
        "name":             control_def["name"],
        "severity":         eval_result.get("severity", control_def["severity"]),
        "status":           eval_result.get("status", "Unknown"),
        # ... rest same
    }
```

### Rule 3: How manual checklist backfill works

```python
# Map ALZ checklist category strings → alz_design_area enum
CHECKLIST_CATEGORY_TO_ALZ: dict[str, ALZDesignArea] = {
    "Network Topology and Connectivity":          "network_topology_and_connectivity",
    "Identity and Access Management":             "identity_and_access_management",
    "Governance":                                 "governance",
    "Management":                                 "management",
    "Security":                                   "security",
    "Data Protection":                            "data_protection",  # if present
    "Resilience":                                 "resilience",       # if present
    "Cost":                                       "cost",             # if present
    "Resource Organization":                      "resource_organization",
    "Platform Automation and DevOps":             "platform_automation_and_devops",
    "Azure Billing and Microsoft Entra ID Tenants": "azure_billing_and_entra_tenant",
}

# In adapter manual backfill:
checklist_category = item.get("category", "Unknown")
area = CHECKLIST_CATEGORY_TO_ALZ.get(checklist_category, "governance")  # safe default
# Now use same taxonomy.scoring_section(area), etc.
```

### Rule 4: Initiative / remediation program grouping

When the AI generates roadmap initiatives, each initiative references one or more `alz_design_area` values. The grouping uses `ALZ_TO_INITIATIVE_GROUP`:

```python
# 8 possible initiative groups (deduped from 11 design areas):
#   "Network Architecture"
#   "Identity & Zero Trust"
#   "Governance & Compliance"        ← governance + resource_organization + billing
#   "Observability & Operations"
#   "Security Posture"
#   "Data Security"
#   "Business Continuity"
#   "FinOps & Optimization"          ← cost + billing
#   "Platform Engineering"
```

This is a **many-to-fewer mapping** — intentional. Multiple design areas can roll into one initiative program.

## Validation Strategy

### 1. JSON Schema validation at load time

```python
# control_packs/loader.py — add at pack load:
import jsonschema

CONTROL_SCHEMA = json.load(open("schemas/control_definition.schema.json"))

def validate_control(ctrl: dict) -> None:
    jsonschema.validate(ctrl, CONTROL_SCHEMA)

# In ControlPack.load():
for cid, ctrl in controls.items():
    validate_control(ctrl)  # raises on bad taxonomy
```

### 2. Enum enforcement (lint rule)

```python
# schemas/taxonomy.py
VALID_DESIGN_AREAS = frozenset(ALZ_DESIGN_AREA_LABELS.keys())

def validate_design_area(area: str) -> None:
    if area not in VALID_DESIGN_AREAS:
        raise ValueError(
            f"Invalid alz_design_area '{area}'. "
            f"Must be one of: {sorted(VALID_DESIGN_AREAS)}"
        )
```

### 3. CI pre-commit hook

```yaml
# .pre-commit-config.yaml
- repo: local
  hooks:
    - id: validate-controls
      name: Validate control definitions
      entry: python -m schemas.validate_controls
      language: system
      files: 'control_packs/.*/controls\.json$'
```

```python
# schemas/validate_controls.py
"""Pre-commit hook: validate all control pack definitions against schema."""
import sys, json, jsonschema, pathlib

SCHEMA = json.loads(pathlib.Path("schemas/control_definition.schema.json").read_text())

def main():
    errors = 0
    for f in pathlib.Path("control_packs").rglob("controls.json"):
        pack = json.loads(f.read_text())
        for cid, ctrl in pack.get("controls", {}).items():
            try:
                jsonschema.validate(ctrl, SCHEMA)
            except jsonschema.ValidationError as e:
                print(f"FAIL {f}:{cid} — {e.message}")
                errors += 1
    sys.exit(1 if errors else 0)

if __name__ == "__main__":
    main()
```

### 4. Test-time graph/pack consistency check

```python
# In test_preflight_agent.py — add:
def test_all_controls_have_valid_taxonomy():
    """Every control in every pack must have a valid alz_design_area."""
    from schemas.taxonomy import VALID_DESIGN_AREAS
    for pack in all_packs:
        for cid, ctrl in pack.controls.items():
            assert ctrl["alz_design_area"] in VALID_DESIGN_AREAS, \
                f"{cid} has invalid alz_design_area: {ctrl['alz_design_area']}"
```

### 5. Uniqueness constraints

| Constraint | Enforcement |
|---|---|
| `control_id` unique across all packs | Schema validator + test |
| `control_id` pattern `^[A-Z]{1,3}[0-9]{2}\.[0-9]{2}$` | JSON Schema `pattern` |
| `alz_design_area` in enum | JSON Schema `enum` + Python `Literal` |
| `evidence_sources[].signal_category` in enum | JSON Schema `enum` |
| `data_driven` → requires `evidence_sources` + `evaluator_module` | JSON Schema `allOf`/`if`/`then` |
| `questionnaire` → requires `question_template` | JSON Schema `allOf`/`if`/`then` |

### What gets deleted after migration

| Current file | Hardcoded dict | Replaced by |
|---|---|---|
| `engine/adapter.py` | `_DESIGN_AREA_SECTION` (6 entries) | `taxonomy.scoring_section()` |
| `engine/scoring.py` | `DOMAIN_WEIGHTS` (8 entries) | `taxonomy.DOMAIN_WEIGHTS` |
| `ai/build_advisor_payload.py` | `_SECTION_TO_DESIGN_AREA` (10 entries) | `taxonomy.design_area_label()` |
| `reporting/render.py` | `_DOMAIN_BUCKETS` (5 entries) | `taxonomy.platform_domain()` grouping |
| `reporting/render.py` | `_MODE_SECTIONS` (5 entries) | filter by `alz_design_area` enum directly |

---

# Scalable Results Data Model

## Model Choice

**Recommended: Hybrid JSON-document model with relational normalization.**

Rationale:
- The tool already produces JSON run files — maintaining compatibility
- No external database dependency (stays CLI-first)
- Structured enough for 100+ subscription aggregation without 27k-row explosion
- Can be trivially loaded into SQLite/DuckDB for ad-hoc analysis if needed

The key insight: **separate static definitions from dynamic evaluation, and store per-subscription evidence in a nested array (not top-level rows)**.

## Entity Definitions

### Entity Relationship Diagram

```
assessment_run (1)
  ├── execution_context (1)
  ├── telemetry (1)
  │     ├── phase_timings[]
  │     └── signal_executions[]
  ├── control_results[] (N = number of controls, NOT controls × subscriptions)
  │     ├── per_subscription[] (M = subscriptions where this control was evaluated)
  │     │     └── evidence[] (K = evidence items for this sub)
  │     └── aggregate (computed)
  └── aggregations (1)
        ├── design_area_maturity[]
        ├── risk_findings[]
        └── executive_summary
```

### Table 1: `assessment_run` (one per scan execution)

```jsonc
{
  "run_id": "run-20260217-1430",
  "timestamp": "2026-02-17T14:30:00Z",
  "tool_version": "0.9.0",
  "control_pack_id": "alz-v1.0",
  "control_pack_version": "1.0.0",
  "mode": "live",                          // "live" | "demo" | "delta"
  "scope": {
    "tenant_id": "aaaabbbb-...",
    "tenant_display_name": "Contoso Corp",
    "subscription_ids": ["sub-1", "sub-2", "sub-3"],
    "subscription_count": 3,
    "management_group_root": "/providers/Microsoft.Management/managementGroups/contoso",
    "rbac_scope": "Tenant",
    "rbac_highest_role": "Reader",
    "coverage_percent": 100.0
  },
  "telemetry": { "$ref": "#telemetry" },
  "control_results": [ "$ref": "#control_result[]" ],
  "aggregations": { "$ref": "#aggregations" }
}
```

### Table 2: `telemetry` (provenance — solves the 0 API calls problem)

```jsonc
{
  // ── Phase timings ─────────────────────────────────────
  "phases": [
    { "name": "context",    "start_iso": "...", "duration_sec": 2.3 },
    { "name": "signals",    "start_iso": "...", "duration_sec": 18.7 },
    { "name": "evaluators", "start_iso": "...", "duration_sec": 4.1 },
    { "name": "aggregation","start_iso": "...", "duration_sec": 0.2 },
    { "name": "ai",         "start_iso": "...", "duration_sec": 45.8 },
    { "name": "reporting",  "start_iso": "...", "duration_sec": 1.9 }
  ],
  "total_duration_sec": 73.0,

  // ── Signal execution log (THE MISSING PIECE) ─────────
  "signal_executions": [
    {
      "signal_bus_name": "resource_graph:azure_firewall",
      "signal_category": "resource_graph",
      "provider": "resource_graph",
      "execution_mode": "multi_sub_aggregate",  // or "single_sub", "cached"
      "subscriptions_queried": 3,
      "api_calls": 1,                    // RG can batch subs in one query
      "duration_ms": 340,
      "result_status": "ok",             // "ok" | "error" | "partial"
      "items_returned": 2,
      "cache_hit": false,
      "error": null
    },
    {
      "signal_bus_name": "defender:pricings",
      "signal_category": "defender",
      "provider": "arm",
      "execution_mode": "multi_sub_aggregate",
      "subscriptions_queried": 3,
      "api_calls": 3,                    // ARM = 1 call per sub
      "duration_ms": 890,
      "result_status": "ok",
      "items_returned": 24,
      "cache_hit": false,
      "error": null
    }
    // ... one entry per signal_bus_name actually invoked
  ],

  // ── Aggregate counters (computed from signal_executions) ───
  "totals": {
    "api_calls": 47,                     // sum of signal_executions[].api_calls
    "rg_queries": 12,                    // where signal_category == "resource_graph"
    "arm_calls": 35,                     // where signal_category != "resource_graph"
    "signals_fetched": 26,               // count where cache_hit == false
    "signals_cached": 4,                 // count where cache_hit == true
    "signal_errors": 1,                  // count where result_status == "error"
    "total_duration_ms": 4200            // sum of all durations
  }
}
```

**Why this fixes the "0 API calls" bug:** The current `RunTelemetry` only gets counters from `bus.reset_events()` *after* evaluators complete. If the signal bus event recording is incomplete or the events dict doesn't contain the right keys, counters stay at 0. The new model records at the signal execution layer — each provider logs its own execution. The `totals` block is **computed from `signal_executions[]`**, not independently accumulated.

### Table 3: `control_result` (one per control — NOT per subscription)

This is the critical design: **271 controls × 1 row each**, regardless of subscription count.

```jsonc
{
  "control_id": "N01.02",
  "legacy_id": "e6c4cfd3-...",
  "name": "Azure Firewall deployed in hub VNet",
  "alz_design_area": "network_topology_and_connectivity",

  // ── Derived taxonomy (computed at read time or cached) ────
  "section": "Networking",                  // from taxonomy.scoring_section()
  "platform_domain": "Networking",          // from taxonomy.platform_domain()
  "waf_pillar": ["Reliability"],            // from taxonomy.waf_pillar()

  // ── Evaluation outcome ────────────────────────────────────
  "status": "Partial",                      // Pass|Fail|Partial|Manual|Deferred|Error
  "severity": "High",
  "confidence": "High",
  "confidence_score": 0.85,
  "evaluation_mode": "data_driven",
  "signals_used": ["resource_graph:azure_firewall"],

  // ── Rationale ─────────────────────────────────────────────
  "reason": "Azure Firewall found in 2/3 subscriptions. Sub-3 lacks firewall deployment.",
  "rationale_template_key": "partial",      // which template was used

  // ── Cross-subscription aggregation ────────────────────────
  "subscription_summary": {
    "total_assessed": 3,
    "passing": 2,
    "failing": 1,
    "not_applicable": 0,
    "coverage_pct": 66.7,
    "coverage_display": "2/3 compliant"
  },

  // ── Scope classification (from engine/aggregation.py) ─────
  "scope_level": "Management Group",       // Subscription|Management Group|Tenant
  "scope_pattern": "Moderate Spread",      // Platform Governance Gap|Moderate Spread|Isolated Drift|None

  // ── Per-subscription detail (NESTED, not top-level rows) ──
  "per_subscription": [
    {
      "subscription_id": "sub-1",
      "subscription_name": "Connectivity",
      "status": "Pass",
      "evidence_count": 1,
      "evidence": [
        {
          "type": "resource",
          "resource_id": "/subscriptions/sub-1/resourceGroups/rg-hub/providers/Microsoft.Network/azureFirewalls/fw-hub",
          "resource_type": "Microsoft.Network/azureFirewalls",
          "detail": "Azure Firewall 'fw-hub' deployed in hub VNet",
          "subscription_id": "sub-1"
        }
      ]
    },
    {
      "subscription_id": "sub-2",
      "subscription_name": "Corp Landing Zone",
      "status": "Pass",
      "evidence_count": 1,
      "evidence": [
        {
          "type": "resource",
          "resource_id": "/subscriptions/sub-2/resourceGroups/rg-hub-2/providers/Microsoft.Network/azureFirewalls/fw-corp",
          "resource_type": "Microsoft.Network/azureFirewalls",
          "detail": "Azure Firewall 'fw-corp' deployed",
          "subscription_id": "sub-2"
        }
      ]
    },
    {
      "subscription_id": "sub-3",
      "subscription_name": "Online Landing Zone",
      "status": "Fail",
      "evidence_count": 0,
      "evidence": []
    }
  ],

  // ── Legacy fields (for backward compatibility) ────────────
  "evidence_count": 2,                     // total across all subs
  "evidence": [                            // flattened top-3 sample for AI/report
    { "resource_id": "...", "detail": "..." },
    { "resource_id": "...", "detail": "..." }
  ]
}
```

### Table 4: `aggregations` (computed rollups)

```jsonc
{
  // ── Per design area maturity ──────────────────────────────
  "design_area_maturity": [
    {
      "alz_design_area": "network_topology_and_connectivity",
      "label": "Network Topology & Connectivity",
      "scoring_section": "Networking",
      "total_controls": 8,
      "pass": 5,
      "fail": 2,
      "partial": 1,
      "manual": 0,
      "deferred": 0,
      "maturity_pct": 68.75,
      "weighted_score": 96.25,          // maturity × domain_weight (1.4)
      "domain_weight": 1.4,
      "subscription_coverage": {
        "avg_pct": 78.3,
        "min_pct": 33.3,                // worst control in this area
        "max_pct": 100.0,
        "controls_below_80pct": 2       // controls with < 80% sub coverage
      },
      "top_gaps": [
        { "control_id": "N01.02", "name": "Azure Firewall in hub", "status": "Partial", "coverage_pct": 66.7 },
        { "control_id": "N04.01", "name": "NSG on all subnets", "status": "Fail", "coverage_pct": 33.3 }
      ]
    }
    // ... one per design area that has controls
  ],

  // ── Risk findings (derived from failing controls) ─────────
  "risk_findings": [
    {
      "finding_id": "RF-001",
      "title": "Network inspection gap in Online Landing Zone",
      "risk_level": "High",
      "alz_design_area": "network_topology_and_connectivity",
      "affected_controls": ["N01.02", "N04.01"],
      "affected_subscriptions": ["sub-3"],
      "scope_pattern": "Isolated Drift",
      "recommendation": "Deploy Azure Firewall or route traffic through hub for subscription sub-3"
    }
  ],

  // ── Executive summary ─────────────────────────────────────
  "executive_summary": {
    "overall_maturity_pct": 62.4,
    "overall_weighted_score": 72.1,
    "total_controls_assessed": 48,
    "automation_coverage_pct": 83.3,     // 40/48 data-driven
    "pass_count": 28,
    "fail_count": 12,
    "partial_count": 5,
    "manual_count": 3,
    "subscriptions_in_scope": 3,
    "strongest_area": { "area": "identity_and_access_management", "maturity_pct": 92.0 },
    "weakest_area": { "area": "cost", "maturity_pct": 33.3 },
    "governance_gap_count": 2,           // controls with scope_pattern = "Platform Governance Gap"
    "tenant_wide_issues": 1              // controls with scope_level = "Tenant"
  }
}
```

## Aggregation Logic

### How to aggregate per control across resources and subscriptions

**Rule: One row per control. Per-subscription detail is nested.**

```python
def aggregate_control(control_id: str, per_sub_results: list[dict]) -> dict:
    """
    Given N per-subscription evaluations for one control,
    produce a single aggregated control_result.
    """
    statuses = [r["status"] for r in per_sub_results]
    passing = sum(1 for s in statuses if s == "Pass")
    failing = sum(1 for s in statuses if s == "Fail")
    total = len(per_sub_results)

    # Aggregate status logic:
    if failing == 0 and passing == total:
        agg_status = "Pass"
    elif passing == 0:
        agg_status = "Fail"
    else:
        agg_status = "Partial"

    return {
        "status": agg_status,
        "subscription_summary": {
            "total_assessed": total,
            "passing": passing,
            "failing": failing,
            "coverage_pct": round(passing / total * 100, 1) if total else 0,
            "coverage_display": f"{passing}/{total} compliant",
        },
        "per_subscription": per_sub_results,
        "evidence_count": sum(r.get("evidence_count", 0) for r in per_sub_results),
    }
```

### How to compute maturity per ALZ design area at enterprise scale

```python
def compute_design_area_maturity(
    controls: list[dict],           # control_results for this area
    domain_weight: float,
) -> dict:
    """
    Maturity for one ALZ design area.

    Formula:
      maturity_pct = (pass_weight + 0.5 * partial_weight) / total_weight * 100
      weighted_score = maturity_pct * domain_weight

    Where weight per control = SEVERITY_WEIGHT[severity]
    """
    SEVERITY_WEIGHT = {"Critical": 3.0, "High": 2.0, "Medium": 1.0, "Low": 0.5}

    total_weight = 0
    pass_weight = 0
    partial_weight = 0

    for ctrl in controls:
        w = SEVERITY_WEIGHT.get(ctrl["severity"], 1.0)
        total_weight += w
        if ctrl["status"] == "Pass":
            pass_weight += w
        elif ctrl["status"] == "Partial":
            partial_weight += w

    maturity_pct = (pass_weight + 0.5 * partial_weight) / total_weight * 100 if total_weight else 0
    return {
        "maturity_pct": round(maturity_pct, 1),
        "weighted_score": round(maturity_pct * domain_weight, 2),
        "domain_weight": domain_weight,
        "total_controls": len(controls),
        "pass": sum(1 for c in controls if c["status"] == "Pass"),
        "fail": sum(1 for c in controls if c["status"] == "Fail"),
        "partial": sum(1 for c in controls if c["status"] == "Partial"),
    }
```

### How to avoid double-counting when a control has multiple evidences

**Rule: Evidence contributes to exactly ONE control_result per subscription.**

```
Evidence → belongs to → per_subscription[sub_id] → belongs to → control_result[control_id]
```

A control evaluated against 3 subscriptions with 2 evidence items each = 6 evidence rows total, but:
- Still **1 control_result row**
- Each evidence belongs to exactly one `per_subscription` entry
- `evidence_count` at the control level = sum of per-sub counts (for display)
- **Scoring uses the control's aggregate `status`, NOT per-evidence counting**

```
                            control N01.02
                           status = "Partial"
                          ┌────────┴────────┐
                   sub-1 (Pass)       sub-2 (Pass)       sub-3 (Fail)
                   evidence: 1        evidence: 1        evidence: 0
                         │                  │
                   /sub/sub-1/...     /sub/sub-2/...
```

Maturity scoring counts `N01.02` as **Partial × severity_weight** — one time. Not 3 times.

## Provenance & Telemetry

### The "0 API calls" problem

**Root cause analysis of the current bug:**

1. `RunTelemetry` accumulates counters via `record_signal_events(bus.reset_events())`
2. `SignalBus.reset_events()` returns recorded events — but event recording depends on providers calling `bus.record_event()` during execution
3. If providers don't record events (or the event dict lacks `type: "signal_returned"`), counters stay at 0
4. Phase timings work because they use `time.perf_counter()` directly in `scan.py` — independent of signal bus events

**Solution in new model:**

The telemetry entity has two layers:

| Layer | Source | Current Status | Fix |
|---|---|---|---|
| `phases[]` | `RunTelemetry.start_phase/end_phase` | **Works** (non-zero) | Keep as-is |
| `signal_executions[]` | Must be recorded by each signal provider | **Broken** (events not flowing) | Each provider returns execution metadata in its `SignalResult` |
| `totals` | **Computed** from `signal_executions[]` | Currently hardcoded counters | Derive, don't accumulate |

**Implementation approach:**

```python
# In SignalResult (signals/types.py) — add execution metadata:
@dataclass
class SignalResult:
    status: str
    raw: dict
    # ... existing fields ...
    execution_meta: dict = field(default_factory=dict)
    # execution_meta = {
    #   "api_calls": 3,
    #   "duration_ms": 890,
    #   "subscriptions_queried": 3,
    #   "cache_hit": False,
    #   "items_returned": 24,
    # }

# In scan.py — after signal phase, build signal_executions from bus results:
signal_executions = []
for name, result in bus.all_results():
    signal_executions.append({
        "signal_bus_name": name,
        "signal_category": name.split(":")[0],
        "api_calls": result.execution_meta.get("api_calls", 0),
        "duration_ms": result.execution_meta.get("duration_ms", 0),
        "cache_hit": result.execution_meta.get("cache_hit", False),
        "items_returned": result.execution_meta.get("items_returned", 0),
        "result_status": result.status,
    })
```

Then the HTML report reads `telemetry.totals.api_calls` instead of relying on event accumulation.

## 3-Subscription Example

### Scenario: 3 subscriptions, 2 controls evaluated

```jsonc
{
  "run_id": "run-20260217-1430",
  "timestamp": "2026-02-17T14:30:00Z",
  "tool_version": "0.9.0",
  "control_pack_id": "alz-v1.0",

  "scope": {
    "tenant_id": "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
    "tenant_display_name": "Contoso Corp",
    "subscription_ids": [
      "11111111-1111-1111-1111-111111111111",
      "22222222-2222-2222-2222-222222222222",
      "33333333-3333-3333-3333-333333333333"
    ],
    "subscription_count": 3,
    "coverage_percent": 100.0
  },

  "telemetry": {
    "phases": [
      { "name": "context",    "duration_sec": 1.8 },
      { "name": "signals",    "duration_sec": 12.4 },
      { "name": "evaluators", "duration_sec": 3.2 },
      { "name": "ai",         "duration_sec": 42.0 },
      { "name": "reporting",  "duration_sec": 1.1 }
    ],
    "total_duration_sec": 60.5,
    "signal_executions": [
      {
        "signal_bus_name": "resource_graph:azure_firewall",
        "signal_category": "resource_graph",
        "api_calls": 1,
        "duration_ms": 340,
        "subscriptions_queried": 3,
        "cache_hit": false,
        "items_returned": 2,
        "result_status": "ok"
      },
      {
        "signal_bus_name": "defender:pricings",
        "signal_category": "defender",
        "api_calls": 3,
        "duration_ms": 890,
        "subscriptions_queried": 3,
        "cache_hit": false,
        "items_returned": 24,
        "result_status": "ok"
      }
    ],
    "totals": {
      "api_calls": 4,
      "rg_queries": 1,
      "arm_calls": 3,
      "signals_fetched": 2,
      "signals_cached": 0,
      "signal_errors": 0
    }
  },

  "control_results": [
    {
      "control_id": "N01.02",
      "name": "Azure Firewall deployed in hub VNet",
      "alz_design_area": "network_topology_and_connectivity",
      "section": "Networking",
      "status": "Partial",
      "severity": "High",
      "confidence": "High",
      "confidence_score": 0.85,
      "signals_used": ["resource_graph:azure_firewall"],
      "reason": "Azure Firewall found in 2/3 subscriptions.",
      "subscription_summary": {
        "total_assessed": 3,
        "passing": 2,
        "failing": 1,
        "coverage_pct": 66.7,
        "coverage_display": "2/3 compliant"
      },
      "scope_level": "Management Group",
      "scope_pattern": "Moderate Spread",
      "per_subscription": [
        {
          "subscription_id": "11111111-1111-1111-1111-111111111111",
          "subscription_name": "Connectivity",
          "status": "Pass",
          "evidence_count": 1,
          "evidence": [{
            "resource_id": "/subscriptions/11111111-.../azureFirewalls/fw-hub",
            "detail": "Azure Firewall 'fw-hub' in hub VNet"
          }]
        },
        {
          "subscription_id": "22222222-2222-2222-2222-222222222222",
          "subscription_name": "Corp LZ",
          "status": "Pass",
          "evidence_count": 1,
          "evidence": [{
            "resource_id": "/subscriptions/22222222-.../azureFirewalls/fw-corp",
            "detail": "Azure Firewall 'fw-corp' deployed"
          }]
        },
        {
          "subscription_id": "33333333-3333-3333-3333-333333333333",
          "subscription_name": "Online LZ",
          "status": "Fail",
          "evidence_count": 0,
          "evidence": []
        }
      ],
      "evidence_count": 2
    },
    {
      "control_id": "S03.14",
      "name": "Defender for Cloud enabled across workloads",
      "alz_design_area": "security",
      "section": "Security",
      "status": "Pass",
      "severity": "Critical",
      "confidence": "High",
      "confidence_score": 0.95,
      "signals_used": ["defender:pricings", "defender:secure_score"],
      "reason": "Defender enabled across all 3 subscriptions with all plans active.",
      "subscription_summary": {
        "total_assessed": 3,
        "passing": 3,
        "failing": 0,
        "coverage_pct": 100.0,
        "coverage_display": "3/3 compliant"
      },
      "scope_level": "Tenant",
      "scope_pattern": "None",
      "per_subscription": [
        {
          "subscription_id": "11111111-1111-1111-1111-111111111111",
          "subscription_name": "Connectivity",
          "status": "Pass",
          "evidence_count": 8,
          "evidence": [
            { "detail": "Defender for Servers: Enabled (P2)" },
            { "detail": "Defender for Storage: Enabled" },
            { "detail": "Defender for SQL: Enabled" },
            { "detail": "Defender for App Service: Enabled" },
            { "detail": "Defender for Key Vault: Enabled" },
            { "detail": "Defender for ARM: Enabled" },
            { "detail": "Defender for DNS: Enabled" },
            { "detail": "Defender for Containers: Enabled" }
          ]
        },
        {
          "subscription_id": "22222222-2222-2222-2222-222222222222",
          "subscription_name": "Corp LZ",
          "status": "Pass",
          "evidence_count": 8,
          "evidence": [
            { "detail": "Defender for Servers: Enabled (P2)" },
            { "detail": "Defender for Storage: Enabled" }
          ]
        },
        {
          "subscription_id": "33333333-3333-3333-3333-333333333333",
          "subscription_name": "Online LZ",
          "status": "Pass",
          "evidence_count": 8,
          "evidence": [
            { "detail": "Defender for Servers: Enabled (P1)" },
            { "detail": "Defender for Storage: Enabled" }
          ]
        }
      ],
      "evidence_count": 24
    }
  ]
}
```

## Executive Rollup Example

Given the 2-control, 3-subscription scenario above:

```jsonc
{
  "aggregations": {
    "design_area_maturity": [
      {
        "alz_design_area": "network_topology_and_connectivity",
        "label": "Network Topology & Connectivity",
        "scoring_section": "Networking",
        "total_controls": 1,
        "pass": 0,
        "fail": 0,
        "partial": 1,
        "maturity_pct": 50.0,
        "weighted_score": 70.0,
        "domain_weight": 1.4,
        "subscription_coverage": {
          "avg_pct": 66.7,
          "min_pct": 66.7,
          "max_pct": 66.7,
          "controls_below_80pct": 1
        },
        "top_gaps": [
          {
            "control_id": "N01.02",
            "name": "Azure Firewall deployed in hub VNet",
            "status": "Partial",
            "coverage_pct": 66.7
          }
        ]
      },
      {
        "alz_design_area": "security",
        "label": "Security",
        "scoring_section": "Security",
        "total_controls": 1,
        "pass": 1,
        "fail": 0,
        "partial": 0,
        "maturity_pct": 100.0,
        "weighted_score": 150.0,
        "domain_weight": 1.5,
        "subscription_coverage": {
          "avg_pct": 100.0,
          "min_pct": 100.0,
          "max_pct": 100.0,
          "controls_below_80pct": 0
        },
        "top_gaps": []
      }
    ],

    "risk_findings": [
      {
        "finding_id": "RF-001",
        "title": "Firewall gap in Online Landing Zone",
        "risk_level": "High",
        "alz_design_area": "network_topology_and_connectivity",
        "affected_controls": ["N01.02"],
        "affected_subscriptions": ["33333333-3333-3333-3333-333333333333"],
        "scope_pattern": "Moderate Spread",
        "recommendation": "Deploy Azure Firewall or configure UDR to route traffic through hub for Online LZ subscription"
      }
    ],

    "executive_summary": {
      "overall_maturity_pct": 75.0,
      "overall_weighted_score": 110.0,
      "total_controls_assessed": 2,
      "automation_coverage_pct": 100.0,
      "pass_count": 1,
      "fail_count": 0,
      "partial_count": 1,
      "manual_count": 0,
      "subscriptions_in_scope": 3,
      "strongest_area": {
        "area": "security",
        "maturity_pct": 100.0
      },
      "weakest_area": {
        "area": "network_topology_and_connectivity",
        "maturity_pct": 50.0
      },
      "governance_gap_count": 0,
      "tenant_wide_issues": 0
    }
  }
}
```

### How the HTML report uses this

```
┌────────────────────────────────────────────────┐
│  Assessment Provenance                          │
├────────────────────────────────────────────────┤
│  Duration:      60.5s                           │  ← telemetry.total_duration_sec
│  API Calls:     4                               │  ← telemetry.totals.api_calls
│  Signals:       2 fetched, 0 cached, 0 errors   │  ← telemetry.totals.*
│  Subscriptions: 3 in scope (100% coverage)      │  ← scope.subscription_count + coverage_percent
└────────────────────────────────────────────────┘
```

No more zeros — because `totals` is computed from `signal_executions[]`, not accumulated from events that may not fire.

---

## Migration Path

### Phase 1: Taxonomy module (non-breaking)
1. Create `schemas/taxonomy.py` with all mapping dicts
2. Update `engine/adapter.py` to import from `taxonomy` (delete `_DESIGN_AREA_SECTION`)
3. Update `engine/scoring.py` to import `DOMAIN_WEIGHTS` from `taxonomy`
4. Update `ai/build_advisor_payload.py` to use `taxonomy` (delete `_SECTION_TO_DESIGN_AREA`)
5. Update `reporting/render.py` to use `taxonomy` (delete `_DOMAIN_BUCKETS`, `_MODE_SECTIONS`)
6. **Result:** Same behavior, single source of truth

### Phase 2: Control schema migration
1. Create `schemas/control_definition.schema.json`
2. Rewrite `control_packs/alz/v1.0/controls.json` to new schema
3. Assign stable `control_id` (N01.01, S03.14, etc.) and keep `legacy_id` for linkage
4. Add `evaluation_mode`, `evidence_sources`, `question_template`, `rationale_template`
5. Update `control_packs/loader.py` to validate on load
6. Update `engine/adapter.py` to use new fields

### Phase 3: Results data model
1. Restructure `control_result` dict to include `per_subscription[]` and `subscription_summary`
2. Add `signal_executions[]` to telemetry
3. Compute `telemetry.totals` from `signal_executions[]`
4. Build `aggregations` block in `scan.py`
5. Update `reporting/render.py` to read new provenance structure
6. Update `reporting/csa_workbook.py` to read `alz_design_area` directly (not checklist lookup)

### Phase 4: Validation & CI
1. Add JSON Schema validation to control pack loader
2. Add pre-commit hook for control definitions
3. Add test for taxonomy consistency
4. Delete all hardcoded mapping dicts (5 files)
