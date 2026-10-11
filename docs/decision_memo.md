# Architecture Decision Memo

## Decision

Ship **Architecture B, the staged analyst -> reviewer variant**, with the same deterministic policy engine and guardrails.

---

## Evidence

Both architectures ran the same 15 cases (6 public, 4 other dataset requests, 5 synthetic). Offline (`summary_offline.md`) both score 15/15 on every metric, because overlap is a heuristic there. The live run (`summary_live.md`, pinned to `groq:openai/gpt-oss-120b`, all 30 rows in `llm` mode, one run per case) separates them:

| Live metric | Single | Staged |
|---|---:|---:|
| Correct next action | 13/15 | 14/15 |
| Policy rules (exact approvals/flags) | 14/15 | 15/15 |
| Human escalation correct | 14/15 | 15/15 |
| Public checks, grounding, API down | all pass | all pass |
| Avg / p95 latency | 37.4 / 55.4 s | 23.3 / 31.8 s |
| LLM calls / request | 4.87 | 2.00 |

A missed EXT-03: it sent TaskFlow Pro to standard approval instead of redirecting to the existing TaskFlow licence and dropped the overlap flag. B got it right. Both missed SYN-01, holding a duplicate new vendor for verification: safe, but a shared gap.

---

## Trade-offs

Code makes every high-stakes call: thresholds, budget, Security/Privacy/Legal triggers, the 365-day expiry against the policy reference date, registry conflicts and API outages. The LLM only judges whether an existing tool covers the need, and writes the explanation.

Live, A's expected cost advantage disappears: gpt-oss makes one tool call per turn, so A averaged 4.87 LLM calls against B's fixed two. B used 59% fewer calls, had 38% lower average and 43% lower p95 latency, and has a predictable quota footprint, which matters given how often this project hit rate limits.

B costs a second prompt, a handoff format and two partial-failure paths. In return the reviewer re-checks the overlap call, the only model step that changes outcomes; EXT-03, the one differing case, was exactly that.

---

## Risks / limitations

- The live evidence is one run on one model over 15 cases, and the accuracy gap is a single case. Repeat with `--repeats 3`, run Gemini live, and add human-labelled overlap cases (SYN-01 shows both still under-call redundant new vendors).
- The regex injection scan can miss paraphrased attacks. The damage is limited because the model cannot change approvals.
- The keyword rules for data sensitivity need Security/Privacy sign-off.
- The review log only lasts for the session. Production needs persistence, auth and an audit trail.

---

## Why this is the right MVP

The client needs reliable evidence gathering, consistent rules, and human decision-making. Both architectures preserve these in code, so the choice comes down to the model workflow. In the live evaluation, B matched or outperformed A on every measured quality metric while using fewer than half the LLM calls and delivering lower average and p95 latency. Despite the added orchestration complexity, B is the better choice for this MVP on the available evidence. Reconsider A if repeated evaluations show that a model with parallel tool-calling capabilities can match B's quality at comparable or lower cost.
