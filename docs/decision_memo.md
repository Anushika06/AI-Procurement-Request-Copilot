# Architecture Decision Memo

## Decision

Ship **Architecture A, the single agent**, with the deterministic policy engine and guardrails it already uses.

## Evidence

Both architectures ran on the same 15 cases: 6 public, the other 4 dataset requests, and 5 synthetic hidden-style requests. Source: `python evals/compare.py` → `evals/results/summary.md`.

| Metric | Single | Staged |
|---|---:|---:|
| Public minimum checks | 6/6 | 6/6 |
| Correct next action | 15/15 | 15/15 |
| Grounded evidence | 15/15 | 15/15 |
| Policy rules (exact approvals/flags) | 15/15 | 15/15 |
| Human escalation correct | 15/15 | 15/15 |
| Vendor API down, safe result | 15/15 | 15/15 |
| Avg latency (offline) | ~20 ms | ~22 ms |
| LLM calls / request | 2-3 | 2 |
| Tool calls / request | 5 | 5 |

These numbers were recorded in offline mode because no API key was available. Offline, the overlap judgement is a heuristic, so the two architectures give identical outputs. The LLM call counts are structural rather than measured. Scripted-model tests (`tests/test_agents.py`) cover LLM-mode behaviour: skipped tools still run, approval claims are discarded, overlap with no catalog match is rejected, and model failures fall back safely.

## Trade-offs

Code makes every high-stakes call: thresholds, budget, Security/Privacy/Legal triggers, the 365-day expiry against the policy reference date, registry conflicts and API outages. The LLM only judges whether an existing tool covers the need, and writes the explanation.

B's reviewer was meant to own policy and risk, but the policy engine already does that, and the reviewer cannot override it. What it actually does is re-check the analyst's overlap call. It never changed approvals, flags or the next action. What it adds is a second prompt, a handoff format and two partial-failure paths.

B's fixed tool plan and fixed call count are real advantages. A's guard closes the tool gap, though, and batching tool calls keeps A at about two LLM calls.

## Risks / limitations

- The overlap judgement in LLM mode is still unmeasured. Re-run `compare.py` with a Gemini key and add human-labelled overlap cases.
- The regex injection scan can miss paraphrased attacks. The damage is limited because the model cannot change approvals.
- The keyword rules for data sensitivity need Security/Privacy sign-off.
- The review log only lasts for the session. Production needs persistence, auth and an audit trail.

## Why this is the right MVP

The client needs reliable evidence gathering, consistent rules and a human making the decision. One agent on top of a deterministic engine delivers all three, with one prompt to maintain and one place to debug. The second agent added no correctness on the same test set, and a simpler system that performs as well is the better one to ship. Revisit B only if live runs show overlap mistakes that a reviewer stage would have caught.
