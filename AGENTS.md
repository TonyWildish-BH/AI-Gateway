# AGENTS.md

This document describes the key directories in the AI Gateway workspace for AI coding agents.

## Overview

The **AI Gateway** is an experimental repository exploring the AI Gateway pattern through Azure API Management. It focuses on managing AI services APIs with security, reliability, performance, and cost controls. Labs use Jupyter notebooks with Python, Bicep templates, and Azure API Management policies.

---

## Directory Structure

### `labs/`

Contains hands-on experimental labs, each in its own subdirectory. Labs are structured as Jupyter notebooks with supporting Bicep infrastructure files and APIM policies.

**Categories of labs include:**

- **AI Agents & MCP**: `model-context-protocol/`, `mcp-client-authorization/`, `mcp-a2a-agents/`, `mcp-from-api/`, `mcp-prm-oauth/`, `mcp-registry-apic/`, `openai-agents/`, `ai-agent-service/`, `realtime-mcp-agents/`, `gemini-mcp-agents/`
- **Model Integration**: `ai-foundry-sdk/`, `ai-foundry-deepseek/`, `ai-foundry-private-mcp/`, `gemini-models/`, `aws-bedrock/`, `slm-self-hosting/`
- **Load Balancing & Routing**: `backend-pool-load-balancing/`, `backend-pool-load-balancing-tf/`, `model-routing/`
- **Security & Access Control**: `access-controlling/`, `content-safety/`, `private-connectivity/`, `secure-responses-api/`
- **Monitoring & Logging**: `built-in-logging/`, `token-metrics-emitting/`
- **Rate Limiting & Caching**: `token-rate-limiting/`, `semantic-caching/`
- **Specialized Features**: `realtime-audio/`, `image-generation/`, `function-calling/`, `vector-searching/`, `message-storing/`, `session-awareness/`
- **Operations**: `finops-framework/`, `zero-to-production/`

- **Lab structure pattern:**

- `README.md` - README file to describe lab following the standard lab structure.
- `<lab-name>.ipynb` - Main Jupyter notebook with step-by-step instructions
- `clean-up-resources.ipynb` - Jupyter notebooks used to removed resources when the lab is finished
- `main.bicep` - Azure infrastructure deployment template
- `params.json` - Temporary file generated automatically for the bicep deployment. This file will not be commited to the repo.
- `*policy.xml` - Azure API Management policy files
- `pyproject.toml` - Lab-specific Python dependencies (only present when the lab needs deps beyond the root environment).
- `src/` - Supporting source code (when applicable)

Disregard the `labs/_deprecated` folder, as it contains archived labs.

---

### `modules/`

Contains reusable Bicep modules for Azure resource deployment. These are referenced by labs' `main.bicep` files.

| Subdirectory | Purpose |
|--------------|---------|
| `apim/` | Azure API Management deployment modules (v1, v2, v3 versions) |
| `apim-streamable-mcp/` | APIM module with MCP streaming support |
| `apic/` | Azure API Center deployment modules |
| `cognitive-services/` | Azure Cognitive Services deployment modules (v1, v2, v3 versions) |
| `monitor/` | Azure Monitor resource modules |
| `network/` | Networking infrastructure modules |
| `operational-insights/` | Log Analytics workspace modules |

**Supporting files:**

- `azure-roles.json` - Azure RBAC role definitions used across modules

---

### `shared/`

Contains shared Python utilities and reusable code used across multiple labs.

| File/Directory | Purpose |
|----------------|---------|
| `utils.py` | Core utility functions: Azure CLI command execution, resource retrieval, model catalogue resolution, formatted console output (print_ok, print_error, print_info, etc.) |
| `models.json` | **Central model catalogue.** The single source of truth for every model name, publisher, version and SKU used anywhere in the repo |
| `apimtools.py` | `APIMClientTool` class for Azure API Management operations: client initialization, API discovery, subscription key management |
| `snippets/` | Reusable Python code snippets loaded into notebooks via `%load` magic command |
| `mcp-servers/` | Sample MCP server implementations (`weather/`, `spotify/`, `github/`, `oncall/`, `prm-graphapi/`) |

**Snippets usage:**

Labs reference `utils` library using Python:

```python
import os, sys, json
sys.path.insert(1, '../../shared')  # add the shared directory to the Python path
import utils

```

---

## Model catalogue

Every model used by the repo is defined once, in `shared/models.json`. Labs refer to
models by **role** — `chat-small`, `embed-large`, `router` — and never by literal
model name. When a model is deprecated or replaced, the change is a single edit to
the catalogue; no notebook, Bicep template or Terraform config needs touching.

**Catalogue structure:**

| Section | Purpose |
|---------|---------|
| `foundry` | Models deployed through Azure Cognitive Services. Each role carries `name`, `publisher`, `version`, `sku`, `defaultCapacity`, and an advisory `lifecycle` |
| `external` | Models not on Azure (AWS Bedrock, Google Gemini, Ollama, self-hosted SLMs). Each role carries `name` and `provider` |
| `aliases` | Literal model name → role. Lets the tooling tell a contributor which role to use, and lets `utils.models_config()` still resolve a literal name |
| `lifecycleCheckedIn` / `lifecycleCheckedOn` | Which regions the `lifecycle` snapshot was taken in, and when |

**Resolving a role:**

```python
# Python / notebooks
models_config = utils.models_config(('chat-small', {"capacity": 20}))  # for deployment
deployment    = utils.model_name('chat-small')                         # just the name
```

```bicep
// Bicep
param modelName string = loadJsonContent('../../shared/models.json').foundry['chat-small'].name
```

```hcl
# Terraform
locals { model_name = jsondecode(file("../../shared/models.json")).foundry["chat-small"].name }
```

APIM policy XML cannot read the catalogue, so a policy that must name a model uses a
`{placeholder}` and the lab's Bicep fills it in — either with an APIM named value
(`labs/apim-purview-dlp`) or with `replace()` over `loadTextContent`
(`labs/model-routing`). `.xml` under `labs/` is scanned by the guard, so a literal there
fails CI like any other.

`utils.models_config()` prints a warning for any role that is not `GenerallyAvailable`,
so a lab author sees a deprecation before deployment. `utils.validate_model_definitions()`
remains the authoritative check — it queries Azure live at deployment time.

**Adding a model:**

1. Add a role under `foundry` (or `external`) with `name`, `publisher`, `version`, `sku`
   and `defaultCapacity`. Prefer reusing an existing role over adding a near-duplicate.
2. Add the literal name → role entry to `aliases`.
3. Run `python scripts/refresh_model_lifecycle.py` to confirm the model really exists at
   that publisher/version in the `lifecycleCheckedIn` regions, and to fill in `lifecycle`.
4. Reference the role from the lab. Never paste the literal name into a notebook, Bicep
   template or Terraform config.

**Deprecating a model — the whole point:**

Change `name`/`version` on the role in `shared/models.json`. Every lab that uses the role
picks up the new model on its next run. Do not search-and-replace across labs. If a role is
retired outright, leave it in place with a `notes` field explaining the successor so labs
that still pin it fail loudly and informatively rather than silently.

**Scripts:**

| Script | Purpose |
|--------|---------|
| `scripts/check_model_catalog.py` | CI guard. Fails when a hardcoded model literal appears in `labs/**` or `modules/**`. Notebook markdown cells, code comments, URLs and image filenames are ignored. `--fix-suggestions` prints the catalogue lookup that replaces each hit. It never edits files |
| `scripts/refresh_model_lifecycle.py` | Refreshes the advisory `lifecycle` metadata from the live Azure catalogue in each `lifecycleCheckedIn` region. Reports a diff table by default; `--write` updates `shared/models.json`. Needs a logged-in `az`; the subscription ID is looked up at runtime. Only `foundry` is checkable — `external` models are skipped |

Run both from the repository root:

```bash
python scripts/check_model_catalog.py --fix-suggestions
python scripts/refresh_model_lifecycle.py                 # dry run
```

`check_model_catalog.py` runs in CI via `.github/workflows/model-catalog.yaml`. If a
literal genuinely cannot resolve through a role, annotate the line with a justified
pragma — bare pragmas without a reason are ignored and still fail:

```python
model = "gpt-4o-mini"  # model-catalog-allow: pinned by an upstream fixture we do not control
```

---

### `tools/`

Contains standalone utility notebooks and testing tools for use with deployed labs.

| File | Purpose |
|------|---------|
| `tracing.ipynb` | Invoke AI Foundry model APIs with tracing enabled |
| `streaming.ipynb` | Test streaming responses from AI models |
| `rate-limit.ipynb` | Test rate limiting configurations |
| `test-ai-gateway.ipynb` | General AI Gateway testing utility |
| `test-sequence.ipynb` | Sequential testing of API calls |
| `client-oauth.ipynb` | OAuth client authentication testing |
| `mock-server/` | Mock OpenAI API server for development and testing |
| `sample-prompts.json` | Sample prompts for testing |

---

## Key Technologies

- **Language**: Python 3.12+
- **Notebooks**: Jupyter notebooks (`.ipynb`)
- **Infrastructure**: Azure Bicep templates (`.bicep`)
- **Policies**: Azure API Management XML policies (`.xml`)
- **Azure Services**: API Management, Microsoft Foundry, Azure API Center, Azure Monitor and other Azure services

## Prerequisites

- Python 3.12+ — dependencies are managed with [uv](https://docs.astral.sh/uv/). Run `uv sync` at the repo root to create the `.venv` and install everything.
- Azure CLI authenticated to an Azure subscription
- VS Code with Jupyter extension
- Azure subscription with appropriate RBAC permissions
