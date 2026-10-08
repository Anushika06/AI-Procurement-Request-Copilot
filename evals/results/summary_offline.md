# Architecture comparison

Mode: **LLM (gemini-2.5-flash)**. Same 15 cases for both architectures (6 public, 4 remaining dataset requests, 5 synthetic hidden-style requests).

| Metric | Single agent (A) | Staged / 2-agent (B) |
|---|---:|---:|
| Public cases passing minimum checks | 6/6 | 6/6 |
| Correct next action | 15/15 | 15/15 |
| Evidence grounded in tool results | 15/15 | 15/15 |
| Policy / deterministic rules followed | 15/15 | 15/15 |
| Human escalation correct | 15/15 | 15/15 |
| Vendor API down: safe degradation | 15/15 | 15/15 |
| Avg latency (ms) | 3511.0 | 2522.6 |
| p95 latency (ms) | 7157.3 | 2630.4 |
| Avg LLM calls | 0.20 | 0.00 |
| Avg tool calls | 5.00 | 5.00 |

## Per-case results

| Case | Arch | Action | Correct | Grounded | Policy | Escalation | ms | LLM | Tools | Notes |
|---|---|---|---|---|---|---|---:|---:|---:|---|
| PUB-01 | single | standard_approval | yes | yes | yes | yes | 7157.3 | 0 | 5 |  |
| PUB-02 | single | route_for_review | yes | yes | yes | yes | 2623.0 | 0 | 5 |  |
| PUB-03 | single | route_for_review | yes | yes | yes | yes | 6809.5 | 0 | 5 |  |
| PUB-04 | single | route_for_review | yes | yes | yes | yes | 2686.9 | 0 | 5 |  |
| PUB-05 | single | request_clarification | yes | yes | yes | yes | 2546.7 | 0 | 5 |  |
| PUB-06 | single | verify_vendor_evidence | yes | yes | yes | yes | 2585.8 | 0 | 5 |  |
| EXT-01 | single | route_for_review | yes | yes | yes | yes | 2535.0 | 0 | 5 |  |
| EXT-02 | single | verify_vendor_evidence | yes | yes | yes | yes | 2684.4 | 0 | 5 |  |
| EXT-03 | single | use_existing_tool | yes | yes | yes | yes | 7853.7 | 3 | 5 |  |
| EXT-04 | single | standard_approval | yes | yes | yes | yes | 2565.7 | 0 | 5 |  |
| SYN-01 | single | use_existing_tool | yes | yes | yes | yes | 2537.3 | 0 | 5 |  |
| SYN-02 | single | route_for_review | yes | yes | yes | yes | 2525.5 | 0 | 5 |  |
| SYN-03 | single | standard_approval | yes | yes | yes | yes | 2535.6 | 0 | 5 |  |
| SYN-04 | single | request_clarification | yes | yes | yes | yes | 2508.1 | 0 | 5 |  |
| SYN-05 | single | route_for_review | yes | yes | yes | yes | 2510.9 | 0 | 5 |  |
| PUB-01 | staged | standard_approval | yes | yes | yes | yes | 2429.5 | 0 | 5 |  |
| PUB-02 | staged | route_for_review | yes | yes | yes | yes | 2572.5 | 0 | 5 |  |
| PUB-03 | staged | route_for_review | yes | yes | yes | yes | 2424.3 | 0 | 5 |  |
| PUB-04 | staged | route_for_review | yes | yes | yes | yes | 2499.7 | 0 | 5 |  |
| PUB-05 | staged | request_clarification | yes | yes | yes | yes | 2429.0 | 0 | 5 |  |
| PUB-06 | staged | verify_vendor_evidence | yes | yes | yes | yes | 2428.2 | 0 | 5 |  |
| EXT-01 | staged | route_for_review | yes | yes | yes | yes | 2535.7 | 0 | 5 |  |
| EXT-02 | staged | verify_vendor_evidence | yes | yes | yes | yes | 2416.3 | 0 | 5 |  |
| EXT-03 | staged | use_existing_tool | yes | yes | yes | yes | 2556.0 | 0 | 5 |  |
| EXT-04 | staged | standard_approval | yes | yes | yes | yes | 2767.1 | 0 | 5 |  |
| SYN-01 | staged | use_existing_tool | yes | yes | yes | yes | 2491.6 | 0 | 5 |  |
| SYN-02 | staged | route_for_review | yes | yes | yes | yes | 2630.4 | 0 | 5 |  |
| SYN-03 | staged | standard_approval | yes | yes | yes | yes | 2553.5 | 0 | 5 |  |
| SYN-04 | staged | request_clarification | yes | yes | yes | yes | 2508.6 | 0 | 5 |  |
| SYN-05 | staged | route_for_review | yes | yes | yes | yes | 2597.0 | 0 | 5 |  |
