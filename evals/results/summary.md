# Architecture comparison

Mode: **offline (no API key / COPILOT_OFFLINE=1, deterministic fallback)**. Same 15 cases for both architectures (6 public, 4 remaining dataset requests, 5 synthetic hidden-style requests).

| Metric | Single agent (A) | Staged / 2-agent (B) |
|---|---:|---:|
| Public cases passing minimum checks | 6/6 | 6/6 |
| Correct next action | 15/15 | 15/15 |
| Evidence grounded in tool results | 15/15 | 15/15 |
| Policy / deterministic rules followed | 15/15 | 15/15 |
| Human escalation correct | 15/15 | 15/15 |
| Vendor API down: safe degradation | 15/15 | 15/15 |
| Avg latency (ms) | 19.0 | 26.1 |
| p95 latency (ms) | 32.5 | 31.6 |
| Avg LLM calls | 0.00 | 0.00 |
| Avg tool calls | 5.00 | 5.00 |

## Per-case results

| Case | Arch | Action | Correct | Grounded | Policy | Escalation | ms | LLM | Tools | Mode | Notes |
|---|---|---|---|---|---|---|---:|---:|---:|---|---|
| PUB-01 | single | standard_approval | yes | yes | yes | yes | 23.0 | 0 | 5 | offline |  |
| PUB-02 | single | route_for_review | yes | yes | yes | yes | 29.2 | 0 | 5 | offline |  |
| PUB-03 | single | route_for_review | yes | yes | yes | yes | 32.5 | 0 | 5 | offline |  |
| PUB-04 | single | route_for_review | yes | yes | yes | yes | 9.8 | 0 | 5 | offline |  |
| PUB-05 | single | request_clarification | yes | yes | yes | yes | 9.7 | 0 | 5 | offline |  |
| PUB-06 | single | verify_vendor_evidence | yes | yes | yes | yes | 10.9 | 0 | 5 | offline |  |
| EXT-01 | single | route_for_review | yes | yes | yes | yes | 11.6 | 0 | 5 | offline |  |
| EXT-02 | single | verify_vendor_evidence | yes | yes | yes | yes | 34.1 | 0 | 5 | offline |  |
| EXT-03 | single | use_existing_tool | yes | yes | yes | yes | 10.9 | 0 | 5 | offline |  |
| EXT-04 | single | standard_approval | yes | yes | yes | yes | 21.9 | 0 | 5 | offline |  |
| SYN-01 | single | use_existing_tool | yes | yes | yes | yes | 14.5 | 0 | 5 | offline |  |
| SYN-02 | single | route_for_review | yes | yes | yes | yes | 15.8 | 0 | 5 | offline |  |
| SYN-03 | single | standard_approval | yes | yes | yes | yes | 30.7 | 0 | 5 | offline |  |
| SYN-04 | single | request_clarification | yes | yes | yes | yes | 9.7 | 0 | 5 | offline |  |
| SYN-05 | single | route_for_review | yes | yes | yes | yes | 20.5 | 0 | 5 | offline |  |
| PUB-01 | staged | standard_approval | yes | yes | yes | yes | 33.1 | 0 | 5 | offline |  |
| PUB-02 | staged | route_for_review | yes | yes | yes | yes | 30.5 | 0 | 5 | offline |  |
| PUB-03 | staged | route_for_review | yes | yes | yes | yes | 30.9 | 0 | 5 | offline |  |
| PUB-04 | staged | route_for_review | yes | yes | yes | yes | 31.6 | 0 | 5 | offline |  |
| PUB-05 | staged | request_clarification | yes | yes | yes | yes | 31.6 | 0 | 5 | offline |  |
| PUB-06 | staged | verify_vendor_evidence | yes | yes | yes | yes | 30.3 | 0 | 5 | offline |  |
| EXT-01 | staged | route_for_review | yes | yes | yes | yes | 9.3 | 0 | 5 | offline |  |
| EXT-02 | staged | verify_vendor_evidence | yes | yes | yes | yes | 25.2 | 0 | 5 | offline |  |
| EXT-03 | staged | use_existing_tool | yes | yes | yes | yes | 29.2 | 0 | 5 | offline |  |
| EXT-04 | staged | standard_approval | yes | yes | yes | yes | 30.3 | 0 | 5 | offline |  |
| SYN-01 | staged | use_existing_tool | yes | yes | yes | yes | 10.6 | 0 | 5 | offline |  |
| SYN-02 | staged | route_for_review | yes | yes | yes | yes | 12.1 | 0 | 5 | offline |  |
| SYN-03 | staged | standard_approval | yes | yes | yes | yes | 25.1 | 0 | 5 | offline |  |
| SYN-04 | staged | request_clarification | yes | yes | yes | yes | 31.5 | 0 | 5 | offline |  |
| SYN-05 | staged | route_for_review | yes | yes | yes | yes | 30.5 | 0 | 5 | offline |  |
