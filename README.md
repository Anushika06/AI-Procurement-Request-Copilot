# AI Procurement Request Copilot

Internal tool that reviews software/service purchase requests. It gathers evidence (budget, existing tools, vendor registry, external vendor-risk service), applies the procurement policy with deterministic checks, and recommends a next action. A human still makes the decision.

FDE Assessment 3. All data is synthetic and comes from the starter pack.

- Architecture and workflow: [`docs/architecture.md`](docs/architecture.md)
- Decision memo: [`docs/decision_memo.md`](docs/decision_memo.md)
- Evaluation results: [`evals/results/summary.md`](evals/results/summary.md)

## Setup and run

You need Python 3.11 or 3.12. Run every command from the repo root.

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
cp .env.example .env            # Windows: Copy-Item .env.example .env
python verify_setup.py          # should end with PRE-FLIGHT PASSED
```

Start everything with one command:

```bash
python run_local.py
```

This starts the vendor-risk mock API on `http://127.0.0.1:8001` and the UI on `http://127.0.0.1:8501`. Press Ctrl+C to stop both.

### LLM mode vs offline mode

- **LLM mode:** put a Gemini key in `.env` as `GEMINI_API_KEY=...`. You can get one free from Google AI Studio. The default model is `gemini-2.5-flash`. Calls go through Gemini's OpenAI-compatible endpoint with the `openai` SDK, so any other OpenAI-compatible provider also works if you set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`.
- **Offline mode:** used when there is no key, or when `COPILOT_OFFLINE=1`. The tools and the policy engine run exactly the same way. Only the model's judgement (does an existing tool already cover the need, plus the rationale text) is replaced by a keyword heuristic. Offline mode makes the app, tests and evals runnable anywhere and reproducible.

The UI sidebar shows which mode is active.

### Tests and evaluation

```bash
python -m pytest                                        # 35 tests: policy rules, tools, both agents, guardrails
python evals/run_public_evals.py --architecture single  # starter harness, 6 public cases
python evals/run_public_evals.py --architecture staged
python evals/compare.py                                 # both architectures, 15 cases -> evals/results/
```

The eval scripts start the mock API themselves if it isn't already running.

## Product workflow

```text
1 Employee request ─> 2 Understand need ─> 3 Gather evidence ─> deterministic policy checks ─> 4 Recommend ─> 5 Human review
   (queue or form)       (agent)              budget / catalog /       thresholds, triggers,          action +        reviewer records
                                              vendor / risk API        expiry, conflicts, injection   evidence        the decision
```

The UI (Streamlit) has three tabs:

- **Request queue:** pick a request and see its details. The justification is shown as untrusted text. Run the review with architecture A or B.
- **New request:** a form for submitting an ad-hoc request. It goes through the same pipeline.
- **Review log:** the decisions human reviewers recorded in this session.

Each result shows:

- the recommendation, colour-coded by action
- the next step and a short rationale
- approvals required, risk flags and missing information
- an evidence table where every row cites its tool and record (e.g. `software_catalog.csv:SW003`, `Policy §5`)
- a run trace with LLM/tool call counts
- a human review form

The copilot cannot approve anything. Only the human decision is recorded.

## Output

`handle_request(request_id, architecture)` in `src/solution.py` returns a `ProcurementDecision`, which carries:

- `recommendation`
- `evidence`
- `required_approvals`
- `missing_information`
- `risk_flags`
- `next_step`
- `human_review_required` (always `true`)
- `telemetry`

Two optional fields were added: `action`, a machine-readable next-action category, and `rationale`.

The next-action categories are `request_clarification`, `use_existing_tool`, `verify_vendor_evidence`, `route_for_review` and `standard_approval`.

## Architecture

**Design split:**
- **AI:** understands the need and decides whether an existing tool already covers it, then explains the recommendation.
- **Code:** handles thresholds, budget, Security/Privacy/Legal triggers, review expiry, conflicts, injection scanning and tool failures.
- **Human:** approves, makes exceptions and purchases.

**A - Single agent** (`src/single_agent.py`): one Gemini agent with tool calling. It decides which tools to call and returns a JSON judgement. A code guard then runs any tool the agent skipped, so the policy engine always sees full evidence.

**B - Staged** (`src/staged_agents.py`): code gathers evidence in a fixed order. A **Procurement Analyst** (LLM call 1) assesses the need and the overlap. A **Policy/Risk Reviewer** (LLM call 2) checks that against the policy-engine output. The handoff between them is JSON.

Both architectures end in `src/decision.py`, which builds the decision. The model's text is filtered there: embedded instructions and claims like "already approved" are dropped. Approvals, flags and missing information come only from the policy engine.

### Tools

| Tool | Kind | Purpose |
|---|---|---|
| `lookup_budget` | deterministic | requester, department, available budget vs. cost |
| `search_catalog` | deterministic | same-category approved tools, scope coverage, same-vendor products, purchase history |
| `get_vendor_record` | deterministic | internal registry: procurement, security, legal-terms status |
| `check_vendor_risk` | external API | mock vendor-risk service; 404/503/unreachable recorded, never treated as a pass |
| `run_policy_checks` | deterministic | policy §1-§10: missing fields, approval tiers, triggers, 365-day expiry, conflicts, injection scan |

### Code layout

```text
src/
  policy.py          deterministic policy engine (reference date read from the policy file)
  tools.py           the five tools + ToolBox (caching, telemetry, evidence)
  decision.py        next-action selection, guardrails, ProcurementDecision assembly
  single_agent.py    Architecture A
  staged_agents.py   Architecture B
  llm.py, prompts.py Gemini client and prompts
  solution.py        handle_request / analyze_request adapter
  mock_server.py     starts the mock API for evals/tests
app.py               Streamlit UI
evals/compare.py     A vs B comparison on 15 cases
evals/expected_outcomes.json   expected action / approvals / flags per case
tests/               policy, agent (with scripted fake LLM), data and mock API tests
```

## Assumptions

The full list is in [`docs/architecture.md`](docs/architecture.md#assumptions). The main ones:

- Date checks use the policy reference date **2026-09-30**, parsed from `data/procurement_policy.md`. A review exactly 365 days old still counts as current.
- `requested_integrations: []` means none and is complete. `null` means missing. A `data_access_level` of `unknown` means missing.
- A justification that is mostly injected instructions counts as a missing business purpose.
- If the vendor-risk API is unavailable, the vendor is never treated as approved. The request gets Security review, Privacy as a precaution for sensitive data, and the `verify_vendor_evidence` action.
- Cross-region storage of sensitive data triggers both Privacy and Legal.
- A department with no budget row is routed to Finance (`budget_unverified`).
- Overlap is matched on catalog category. A company-wide or same-department tool with no stated gap leads to `use_existing_tool`. A stated gap, or a limited-use approval that doesn't cover the data class, leads to Procurement validation.

## Evaluation results

Both architectures were run on the same set: the 6 public cases, the other 4 dataset requests, and 5 synthetic hidden-style requests. The synthetic ones are an unknown vendor, a department with no budget, injection in a clean request, an unknown requester, and exactly $25,000. Each case is scored against expected action, exact approvals, required and forbidden flags, grounding (every evidence row comes from a tool that ran and cites an existing record) and escalation. The run is repeated with the vendor API forced down.

These results are from offline mode (recorded without an API key):

| Metric | Single agent (A) | Staged / 2-agent (B) |
|---|---:|---:|
| Public cases passing minimum checks | 6/6 | 6/6 |
| Correct next action | 15/15 | 15/15 |
| Evidence grounded in tool results | 15/15 | 15/15 |
| Policy / deterministic rules followed | 15/15 | 15/15 |
| Human escalation correct | 15/15 | 15/15 |
| Vendor API down: safe degradation | 15/15 | 15/15 |
| Avg latency (ms, offline) | ~20 | ~22 |
| Avg LLM calls | 0 offline; 2-3 with LLM (tool loop) | 0 offline; 2 with LLM (fixed) |
| Avg tool calls | 5 | 5 |

Per-case details are in `evals/results/summary.md` and `evals/results/comparison.csv`. The starter harness output is in `evals/results_single.csv` and `evals/results_staged.csv`.

With a key set, `python evals/compare.py` re-runs everything in LLM mode and rewrites the results with measured latency and LLM call counts.

## Architecture comparison and ship decision

On this test set the two architectures are equivalent on every quality metric. The policy engine owns approvals, flags and escalation, and neither architecture's model output can override it. B's reviewer re-checks a decision the engine has already made, and it costs a second prompt, a handoff format and an extra failure path.

**Decision: ship A (single agent + deterministic policy engine).** It has fewer moving parts and the same measured quality. The guard removes A's main risk, which is skipped tools. The full reasoning is in [`docs/decision_memo.md`](docs/decision_memo.md).

## Known limitations

- The reported numbers are from offline mode. The LLM's overlap judgement has not been measured against labelled cases yet.
- The offline overlap heuristic is keyword-based. For example, it treats "advanced" or "additional" in the justification as an expansion.
- Prompt-injection detection is regex-based and will miss paraphrased attacks. The impact is limited because model output cannot change approvals or flags.
- Sensitivity rules use keywords on `data_access_level` and integration names.
- The human review log only lasts for the session: no persistence or authentication.
- The policy engine encodes this policy version (2026.09) in code. A policy change means a code change, with tests.

## Changes to the starter pack

- `run_local.py`:
  - Streamlit now starts headless, so the first run no longer hangs on the email prompt.
  - An already running mock API is reused instead of crashing on the port conflict.
- `evals/run_public_evals.py` starts the mock API if needed. Before this change, every case failed as "vendor-risk unavailable" unless the API was started by hand.
- CSV loaders use `keep_default_na=False`, so blank dates and IDs are empty strings instead of `NaN` floats.
- Added `pytest.ini` (`pythonpath = .`) so `tests/test_mock_api.py` can import `mock_api` under pytest.
- Added `openai` and `pytest` to `requirements.txt`.
- Added an offline `handle_request` smoke test to `verify_setup.py`.
- Evaluation CSVs are no longer git-ignored, so the results are part of the submission.
