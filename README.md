# Azure Landing Zone Assessor (`lz-assessor`)

> **Sample report generated from a non-production lab subscription.**
> No customer data is included.
> All findings are based on synthetic test workloads for demonstration purposes.

A comprehensive, automated Azure Landing Zone assessment tool designed for **Cloud Solution Architects (CSAs)** conducting landing zone assessments. It evaluates all subscriptions visible to the authenticated identity (tenant-wide with appropriate RBAC) against Microsoft's [Azure Landing Zone (ALZ) checklist](https://github.com/Azure/review-checklists), scores controls deterministically, then enriches the results with AI-generated advisory output — producing a ready-to-deliver **CSA workbook**, **executive ALZ Readiness HTML report**, and **target architecture** in a single command.

Designed to support CSA discovery workshops and partner landing zone engagements.

## Why This Matters

Enterprise customers often have a landing zone but **lack a reliable way to measure enterprise-scale readiness and identify the architectural gaps that block transformation**.

This agent:

- Uses **read-only access (safe for customer environments)**
- Evaluates **real platform signals — not questionnaires**
- Selects controls dynamically based on **intent**
- Produces a **defensible, capability-aligned roadmap**

**Run one command → get a scored assessment, platform readiness briefing, and a traceable 30-60-90 plan.**

> [!NOTE]
> 🔎 **Open the interactive demo report:**  
> **[View the HTML assessment report](https://htmlpreview.github.io/?https://github.com/rebmid/Reasoning-Agent-Azure-Landing-Zone-Assessment-Advisor/blob/main/docs/demo/Contoso-ALZ-Platform-Readiness-Report-Sample.html)**

Generated from a real Azure Test/Lab "Contoso" tenant using read-only access.

![Platform Maturity & Top Risks](docs/demo/report-hero.png)

---

### 📊 Platform maturity scored against the ALZ checklist

Every control is evaluated against the official Azure Landing Zone review checklist — evidence-based, not questionnaire-based.

![Automated Control Evaluation](docs/demo/02_automated-control-evaluation-with-evidence.png)

---

### 🔍 Evidence-based assessment scope

Live platform signals with confidence labelling for every dimension.

![Assessment Scope & Confidence](docs/demo/04_data-confidence.png)

---

### 🗺 30-60-90 transformation roadmap

Dependency-ordered initiatives with maturity trajectory projections.

![Roadmap Traceability](docs/demo/03_roadmap-traceability.png)

---

### 🧠 Causal risk analysis

Root cause → cascade impact → initiative that resolves it.

![Causal Risk Analysis](docs/demo/05_causal-risk-analysis-networking.png)

---

### 🔐 Read-only execution context discovery

The assessment auto-discovers tenant scope, RBAC role, and signal availability before evaluation.

![Execution Context](docs/demo/00a_execution-context.png)

## Features

| Capability | Description |
|---|---|
| **Live ALZ Checklist** | Always fetches the latest checklist from the `Azure/review-checklists` GitHub repo — never stale |
| **40 Automated Evaluators** | 39 signal providers across Resource Graph, Defender, Policy, Management Groups, Microsoft Graph, Cost Management, Update Manager, and Monitor — scoring controls as Pass / Fail / Partial. Automation coverage depends on signal availability and RBAC scope. |
| **Weighted Scoring** | Domain-weighted maturity model with severity multipliers across 8 design areas |
| **9-Pass AI Advisory Pipeline** | Roadmap, executive briefing, ALZ pattern selection, sequence justification, readiness, smart questions, implementation backlog, Learn grounding, and target architecture |
| **Microsoft Learn MCP Grounding** | Official MCP SDK retrieves real guidance, code samples, and full documentation |
| **CSA Workbook (Excel)** | Template-based `.xlsm` with 0_Executive_Summary, 1_30-60-90_Roadmap, 2_Control_Details, 3_Risk_Analysis, Values, and ChecklistIndex sheets — formulas and charts intact |
| **ALZ Readiness HTML Report** | Platform readiness report with maturity scores, adoption blockers, and domain deep dive |
| **Delta Tracking** | Shows control-level progress between runs |
| **8 Preflight Probes** | Validates RBAC, Resource Graph, Policy, Defender, Log Analytics, Entra ID, Cost Management, and Microsoft Graph API access before a full scan |
| **Identity & PIM Deep Signals** | When Microsoft Graph Directory.Read.All is available, the assessor scores PIM maturity, break-glass validation, service principal owner risk, and admin Conditional Access coverage |
| **Operations Maturity Signals** | Alert→Action Group mapping, action group coverage, availability monitoring coverage and SLO signal readiness, patch posture, change tracking |
| **Cost Governance Signals** | Forecast vs actual delta, idle resource heuristics based on low utilization signals |
| **Intent-Based Assessment** | Evaluates only relevant controls based on user intent |
| **Enterprise-Scale Multi-Sub** | Tenant-wide assessment across 100+ subscriptions with parallel collectors, query caching, and `--mg-scope` filtering |
| **Control Enrichment** | Post-processing adds Control Source, Derived Control ID, Control Type, and Related ALZ Control IDs to every row |
| **Resilient JSON Parsing** | Model output sanitizer fixes trailing commas, JS comments, and single-quoted strings before parsing — with truncation repair and retry |
| **Pluggable AI Provider** | Swap AOAI for another model in one line |

---

## Project Structure

```
lz-assessor/
├── scan.py                  # Entry point — CLI, orchestration, output assembly
├── requirements.txt         # Python dependencies
├── .env                     # Azure OpenAI keys (git-ignored)
├── demo/                    # Demo fixtures (--demo mode, no Azure required)
├── docs/                    # Documentation assets & demo screenshots
├── alz/                     # ALZ checklist loader (live from GitHub)
├── collectors/              # Azure data collectors (Resource Graph, Defender, Policy, MG, Graph)
├── signals/                 # Signal Bus — 39 providers routing platform telemetry
├── evaluators/              # 40 control evaluators (auto-registered)
├── control_packs/           # Versioned ALZ control pack definitions
├── engine/                  # Scoring engine, assessment runtime, delta tracking
├── graph/                   # Knowledge graph (control → CAF → dependency mappings)
├── ai/                      # 9-pass AI reasoning pipeline + MCP grounding
├── agent/                   # Intent orchestrator, why-risk reasoning, workshop mode
├── discovery/               # Discovery tree definitions for customer workshops
├── preflight/               # Preflight access validation (8 probes)
├── reporting/               # HTML report + CSA workbook (Excel) generators
├── schemas/                 # Shared domain types
└── out/                     # Output directory (git-ignored)
```

---

## Prerequisites

| Requirement | Details |
|---|---|
| **Python** | 3.12 or later |
| **Azure CLI** | Installed and authenticated (`az login`) |
| **Azure Permissions** | Reader role (minimum) on target subscriptions. Management Group Reader for full hierarchy visibility. |
| **Azure OpenAI** | Required for AI features. Needs a `gpt-4.1` deployment (or any chat-completion model). Set env vars (see [Configuration](#configuration)). |
| **Git** | For cloning the repository |

<details>
<summary><strong>Required Azure Resource Providers</strong> (expand for full list)</summary>

The tool queries Azure Resource Graph and ARM APIs using **read-only** calls. Most providers are registered by default — if a signal returns empty, missing provider registration is the most common cause.

| Resource Provider | Signal(s) | Registered by Default? |
|---|---|---|
| `Microsoft.ResourceGraph` | All Resource Graph queries | ✅ Yes |
| `Microsoft.Network` | Firewalls, VNets, Public IPs, NSGs, Route Tables, Private Endpoints, DDoS, Network Watcher | ✅ Yes |
| `Microsoft.Storage` | Storage Account Posture | ✅ Yes |
| `Microsoft.KeyVault` | Key Vault Posture | ✅ Yes |
| `Microsoft.Sql` | SQL Server Posture | Only if SQL is used |
| `Microsoft.Web` | App Service Posture | Only if App Service is used |
| `Microsoft.ContainerRegistry` | Container Registry Posture | Only if ACR is used |
| `Microsoft.ContainerService` | AKS Cluster Posture | Only if AKS is used |
| `Microsoft.RecoveryServices` | VM Backup Coverage | Only if Backup is configured |
| `Microsoft.Compute` | VM inventory (for backup/update coverage) | ✅ Yes |
| `Microsoft.Security` | Defender plans, Secure Score | ✅ Yes |
| `Microsoft.Authorization` | RBAC hygiene, Resource Locks, Policy assignments | ✅ Yes (built-in) |
| `Microsoft.PolicyInsights` | Policy compliance summary | ✅ Yes |
| `Microsoft.Management` | Management Group hierarchy | ✅ Yes |
| `Microsoft.Insights` | Diagnostics coverage, Alert rules, Action groups, Activity log | ✅ Yes |
| `Microsoft.CostManagement` | Cost forecast vs actual, idle resource detection | ✅ Yes |
| `Microsoft.Maintenance` | Update Manager maintenance configurations | Only if Update Manager is used |
| `Microsoft.Graph` | PIM maturity, break-glass accounts, SP owner risk, admin CA coverage | Requires Microsoft Graph API permissions (Directory.Read.All) |

To check registration: `az provider show -n Microsoft.RecoveryServices --query "registrationState" -o tsv`  
To register: `az provider register -n Microsoft.RecoveryServices`

> **Note:** If a resource type doesn't exist in the subscription (e.g., no AKS clusters), the evaluator returns **NotApplicable** — not an error.

</details>

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/rebmid/Reasoning-Agent-Azure-Landing-Zone-Assessment-Advisor.git
cd Reasoning-Agent-Azure-Landing-Zone-Assessment-Advisor
```

### 2. Create a virtual environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Try it immediately — no Azure required

You can explore the full output format using the bundled demo fixture and skipping AI:

```bash
python scan.py --demo --no-ai
```

This runs all evaluators against sample data, generates the HTML report and CSA workbook, and writes everything to `out/` — zero external dependencies.

### 5. Configure Azure OpenAI *(optional — for AI features)*

Create a `.env` file in the project root (this file is git-ignored):

```env
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
AZURE_OPENAI_KEY=<your-api-key>
```

> **Note:** If you skip this step the tool still runs — you just won't get the 9-pass AI advisory output (executive summary, roadmap, etc.). You can add AI later by creating the `.env` file and re-running.

### 6. Authenticate with Azure

```bash
az login
```

If you have multiple tenants, target the correct one:

```bash
az login --tenant <tenant-id>
```

### 7. Run the assessment against your environment

```bash
python scan.py
```

That's it. The tool will:

1. Discover your Azure execution context (tenant, subscriptions, identity)
2. Fetch the latest ALZ checklist from GitHub
3. Run all evaluators against your environment
4. Score every control with weighted domain scoring
5. Generate a 9-pass AI advisory pipeline (if OpenAI is configured)
6. Ground recommendations in Microsoft Learn documentation
7. Output all artifacts to the `out/` directory

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | For AI features | Your Azure OpenAI resource endpoint URL |
| `AZURE_OPENAI_KEY` | For AI features | API key for the Azure OpenAI resource |

### Azure OpenAI Model

The tool defaults to the `gpt-4.1` deployment. To use a different model, modify the `AOAIClient` initialization in `ai/engine/aoai_client.py`.

### API Version

Default: `2024-02-15-preview`. Configurable in `AOAIClient.__init__()`.

---

### CLI Flags

```
python scan.py [OPTIONS]
```

| Flag | Description |
|---|---|
| `--demo` | Run against bundled sample data — no Azure required |
| `--no-ai` | Skip the AI advisory pipeline (deterministic only) |
| `--preflight` | Validate Azure permissions and exit |
| `--why DOMAIN` | Explain why a domain is the top risk (e.g. `--why Networking`) |
| `--mg-scope MG_ID` | Scope assessment to a specific management group |

<details>
<summary>Additional flags</summary>

| Flag | Description |
|---|---|
| `--tenant-wide` | Scan all visible subscriptions (default: Resource Graph subs only) |
| `--on-demand INTENT` | Targeted evaluation via IntentOrchestrator |
| `--workshop` | Interactive discovery workshop to resolve Manual controls |
| `--no-html` | Skip HTML report generation |
| `--pretty` | Pretty-print final JSON to stdout |

</details>

### Examples

```bash
# No Azure required
python scan.py --demo

# Full assessment
az login
python scan.py

# Scope to a management group
python scan.py --mg-scope "Contoso-LandingZones"

# Deterministic only (no AI cost)
python scan.py --no-ai
```

---

## Output Artifacts

All outputs are written to the `out/` directory:

| File | Description |
|---|---|
| `run-YYYYMMDD-HHMM.json` | Complete assessment data — controls, scores, AI output, delta, execution context |
| `Contoso-ALZ-Platform-Readiness-Report-Sample.html` | Interactive platform readiness report with adoption blockers and domain deep dive |
| `CSA_Workbook_v1.xlsm` | Template-based CSA deliverable workbook (see [CSA Workbook](#csa-workbook)) |
| `target_architecture.json` | AI-generated target architecture with component recommendations and Learn references |
| `preflight.json` | *(preflight mode only)* Access probe results |

Additionally, `assessment.json` is written to the project root as a convenience copy.

---

## How It Works

### 1. Data Collection

The **collectors** module queries Azure APIs via Resource Graph, Defender, Policy, and Management Group endpoints to gather raw infrastructure data:

- **Resource Graph** — VNets, firewalls, public IPs, NSGs, route tables, storage accounts, Key Vaults, private endpoints, diagnostic settings, and more
- **Defender** — security score, coverage tier, recommendations
- **Policy** — policy definitions, assignments, and compliance state
- **Management Groups** — full hierarchy tree
- **Microsoft Graph** — PIM role assignments, break-glass accounts, service principal owners, Conditional Access policies
- **Cost Management** — forecast vs actual spend, idle resource detection
- **Monitor** — alert rules, action groups, Log Analytics topology, activity log, availability signals
- **Update Manager** — patch posture with maintenance configuration correlation

All queries use `AzureCliCredential` — the same identity you authenticated with via `az login`.

### 2. Evaluation & Scoring

The **Signal Bus** architecture routes collected data through registered evaluators:

1. The ALZ checklist is fetched live from GitHub (~243 controls across Security, Networking, Governance, Identity, Platform, and Management domains)
2. Each control is matched to one of the 40 registered evaluators (or marked `Manual` if no automated check exists)
3. Evaluators emit `Pass`, `Fail`, `Partial`, or `Info` verdicts with evidence
4. The **scoring engine** applies domain weights and severity multipliers to produce a composite risk score
5. **Automation coverage** is calculated — typically 8-20% of controls have automated evidence depending on signal availability, with the rest requiring customer conversation

### 3. AI Reasoning Engine

When AI is enabled, a **9-pass pipeline** runs against Azure OpenAI:

| Pass | Prompt | Output | max_tokens |
|---|---|---|---|
| 1 | `roadmap.txt` | 30-60-90 transformation roadmap + named initiatives | 8000 |
| 2 | `exec.txt` | Platform readiness briefing with adoption blockers | 8000 |
| 3 | `implementation_decision` | ALZ pattern selection per initiative (MCP-enriched) | 8000 |
| 4 | `sequence_justification` | Why initiatives are ordered this way in platform terms | 8000 |
| 5 | `readiness.txt` | Enterprise-scale readiness assessment | 8000 |
| 6 | `smart_questions.txt` | Customer discovery questions per domain | 8000 |
| 7 | `implementation.txt` × N | Implementation backlog (one item per initiative) | 4000 |
| 8 | *(MCP grounding)* | Learn doc refs, code samples, ALZ design area enrichment | — |
| 9 | `target_architecture.txt` | Target architecture + `grounding.txt` enrichment | 8000 |

The `AOAIClient` includes built-in resilience:
- **JSON sanitiser** — fixes trailing commas, JS comments, and single-quoted strings in model output before parsing
- **JSON fence stripping** — removes markdown ````json```` wrappers from model output
- **Truncation repair** — closes dangling brackets and strings when output is cut off
- **Retry loop** — up to 2 retries on invalid JSON responses

### 4. Grounding via Microsoft Learn MCP

The tool uses the **official MCP Python SDK** (Streamable HTTP transport) to connect to Microsoft's documentation API at `https://learn.microsoft.com/api/mcp`:

| MCP Tool | Purpose |
|---|---|
| `microsoft_docs_search` | Retrieves curated 500-token content chunks for each initiative |
| `microsoft_code_sample_search` | Fetches Bicep/Terraform code samples for infrastructure recommendations |
| `microsoft_docs_fetch` | Downloads full documentation pages as markdown for deep grounding |

If MCP is unreachable, a **fallback** uses the public Learn search REST API (`https://learn.microsoft.com/api/search`) to provide title + URL + description.

Grounding runs for:
- Each initiative in the transformation roadmap
- Each identified gap
- The target architecture

### 5. Report Generation

**HTML Report** (`Contoso-ALZ-Platform-Readiness-Report-Sample.html`):
- Platform readiness snapshot with overall maturity score
- Domain score breakdown with visual indicators
- Landing zone adoption blockers
- Remediation initiative sequence
- Delta changes from previous runs

**CSA Workbook** (`CSA_Workbook_v1.xlsm`):
- See [CSA Workbook](#csa-workbook) below

---

## CSA Workbook

The workbook is the primary **customer-facing deliverable** — a template-based `.xlsm` with formulas and charts pre-built. Python writes **data only** into the existing structure.

| Sheet | Content |
|---|---|
| **0_Executive_Summary** | Engagement framing, assessment metrics, top business risks |
| **1_30-60-90_Roadmap** | Phased transformation plan with initiative IDs, CAF alignment, dependencies, related controls & risks |
| **2_Control_Details** | All ~243 controls with status, evidence, severity, Learn URLs, and discussion points. Enriched with Control Source, Derived Control ID, Control Type, and Related ALZ Control IDs. |
| **3_Risk_Analysis** | Causal risk breakdown by domain (populated when `--why` payloads are present) |
| **Values / ChecklistIndex** | Lookup tables used by template formulas |

The source of truth for checklist data is always the **GitHub ALZ JSON** fetched at runtime — the Excel workbook is a write-only output sink.

---

## On-Demand Evaluation Mode

For targeted workshop assessments, use `--on-demand`:

```bash
python scan.py --on-demand enterprise_readiness
```

This runs the **IntentOrchestrator** which:
1. Loads the ALZ control pack
2. Routes the intent to relevant evaluators
3. Runs the assessment runtime against the targeted scope
4. Optionally generates an AI explanation of the results
5. Saves output to `out/run-*-on-demand.json`

---

## Why-Risk Reasoning (`--why`)

After a full assessment, drill into **why** a specific domain was flagged as the top risk:

```bash
python scan.py --why Networking --demo
```

This runs a **6-step causal reasoning pipeline** over the existing assessment data:

| Step | What it does |
|---|---|
| 1. **Find risk** | Matches the domain to a top business risk from the executive summary |
| 2. **Failing controls** | Extracts every Fail/Partial control tied to the risk |
| 3. **Dependency impact** | Queries the knowledge graph for downstream controls blocked by failures |
| 4. **Roadmap initiatives** | Finds transformation plan actions that address the affected controls |
| 5. **Learn grounding** | Attaches Microsoft Learn references to each initiative via MCP |
| 6. **AI causal explanation** | Sends the assembled evidence to the reasoning model for root-cause analysis |

The AI output includes:
- **Root cause** — why the domain is the top platform gap (current-state framing)
- **Platform impact** — specific consequences tied to the evidence
- **Fix sequence** — ordered remediation steps with dependency rationale and Learn URLs
- **Cascade effect** — which downstream controls will automatically improve

Output is saved to `out/why-{domain}.json`. Use `--no-ai` to get the raw evidence payload without the AI narration.

---

## Preflight Mode

Before running a full assessment, validate your Azure permissions:

```bash
python scan.py --preflight
```

Preflight probes check:
- Subscription visibility
- Resource Graph query access
- Management group read access
- Defender API access
- Policy read access
- Log Analytics workspace access
- Entra ID diagnostic log access
- Cost Management API access
- Microsoft Graph API access

Results are saved to `out/preflight.json` and printed to the console with pass/fail indicators.

---

<details>
<summary><strong>Scoring Model</strong> (expand for weights and multipliers)</summary>

### Domain Weights

| Domain | Weight |
|---|---|
| Security | 1.5× |
| Networking | 1.4× |
| Identity | 1.4× |
| Governance | 1.3× |
| Platform | 1.2× |
| Management | 1.1× |

### Status Multipliers

Fail = 1.0× | Partial = 0.6× | Pass / Manual = 0×

**Composite risk score** = Σ (severity_weight × status_multiplier × domain_weight) for all controls

</details>

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `AZURE_OPENAI_KEY / AZURE_OPENAI_ENDPOINT not set` | Create a `.env` file with your Azure OpenAI credentials, or run with `--no-ai` |
| `No subscriptions found` | Ensure `az login` succeeded and your identity has Reader on at least one subscription |
| `Management group hierarchy not visible` | Your identity needs Management Group Reader — the tool still works, but MG-related controls will be `Manual` |
| `Unterminated string` / JSON parse errors in AI output | The tool auto-repairs truncated JSON and sanitises common LLM quirks (trailing commas, comments). If it persists, check your Azure OpenAI quota and model deployment |
| `MCP connection failed` | The tool falls back to the public Learn search API automatically. No action needed. |
| `ModuleNotFoundError` | Ensure your virtual environment is activated and `pip install -r requirements.txt` completed successfully |
| `az: command not found` | Install the [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| Slow execution | Large tenants take longer. Use `--tenant-wide` only when needed. AI passes add ~60-90s. |

## Built with AI Assistance

Built with AI assistance: GitHub Copilot was used for code generation, refactoring, and test scaffolding. All architecture, control logic, Azure integration, and reasoning workflows were designed and implemented by the author.

## License

MIT License © 2026 Rebekah Midkiff