# Architecture comparison

Mode: **llm (groq:openai/gpt-oss-120b)**. Same 15 cases for both architectures (6 public, 4 remaining dataset requests, 5 synthetic hidden-style requests).

| Metric | Single agent (A) | Staged / 2-agent (B) |
|---|---:|---:|
| Public cases passing minimum checks | 6/6 | 6/6 |
| Correct next action | 13/15 | 14/15 |
| Evidence grounded in tool results | 15/15 | 15/15 |
| Policy / deterministic rules followed | 14/15 | 15/15 |
| Human escalation correct | 14/15 | 15/15 |
| Vendor API down: safe degradation | 15/15 | 15/15 |
| Avg latency (ms) | 37356.7 | 23342.9 |
| p95 latency (ms) | 55439.4 | 31778.0 |
| Avg LLM calls | 4.87 | 2.00 |
| Avg tool calls | 5.00 | 5.00 |

## Per-case results

| Case | Arch | Action | Correct | Grounded | Policy | Escalation | ms | LLM | Tools | Mode | Notes |
|---|---|---|---|---|---|---|---:|---:|---:|---|---|
| PUB-01 | single | standard_approval | yes | yes | yes | yes | 20126.4 | 4 | 5 | llm |  |
| PUB-02 | single | route_for_review | yes | yes | yes | yes | 55439.4 | 5 | 5 | llm |  |
| PUB-03 | single | route_for_review | yes | yes | yes | yes | 39247.3 | 4 | 5 | llm |  |
| PUB-04 | single | route_for_review | yes | yes | yes | yes | 81456.0 | 5 | 5 | llm |  |
| PUB-05 | single | request_clarification | yes | yes | yes | yes | 44222.5 | 6 | 5 | llm |  |
| PUB-06 | single | verify_vendor_evidence | yes | yes | yes | yes | 47177.6 | 5 | 5 | llm |  |
| EXT-01 | single | route_for_review | yes | yes | yes | yes | 28073.4 | 5 | 5 | llm |  |
| EXT-02 | single | verify_vendor_evidence | yes | yes | yes | yes | 34372.9 | 5 | 5 | llm |  |
| EXT-03 | single | standard_approval | **no** | yes | **no** | **no** | 41361.1 | 5 | 5 | llm | action standard_approval != use_existing_tool | missing flag existing_tool_overlap |
| EXT-04 | single | standard_approval | yes | yes | yes | yes | 33512.1 | 5 | 5 | llm |  |
| SYN-01 | single | verify_vendor_evidence | **no** | yes | yes | yes | 23169.8 | 4 | 5 | llm | action verify_vendor_evidence != use_existing_tool |
| SYN-02 | single | route_for_review | yes | yes | yes | yes | 27432.2 | 5 | 5 | llm |  |
| SYN-03 | single | standard_approval | yes | yes | yes | yes | 27347.2 | 5 | 5 | llm |  |
| SYN-04 | single | request_clarification | yes | yes | yes | yes | 29007.0 | 5 | 5 | llm |  |
| SYN-05 | single | route_for_review | yes | yes | yes | yes | 28405.9 | 5 | 5 | llm |  |
| PUB-01 | staged | standard_approval | yes | yes | yes | yes | 8053.1 | 2 | 5 | llm |  |
| PUB-02 | staged | route_for_review | yes | yes | yes | yes | 9507.3 | 2 | 5 | llm |  |
| PUB-03 | staged | route_for_review | yes | yes | yes | yes | 16113.8 | 2 | 5 | llm |  |
| PUB-04 | staged | route_for_review | yes | yes | yes | yes | 31778.0 | 2 | 5 | llm |  |
| PUB-05 | staged | request_clarification | yes | yes | yes | yes | 24002.1 | 2 | 5 | llm |  |
| PUB-06 | staged | verify_vendor_evidence | yes | yes | yes | yes | 28239.4 | 2 | 5 | llm |  |
| EXT-01 | staged | route_for_review | yes | yes | yes | yes | 29349.8 | 2 | 5 | llm |  |
| EXT-02 | staged | verify_vendor_evidence | yes | yes | yes | yes | 26717.9 | 2 | 5 | llm |  |
| EXT-03 | staged | use_existing_tool | yes | yes | yes | yes | 34140.2 | 2 | 5 | llm |  |
| EXT-04 | staged | standard_approval | yes | yes | yes | yes | 16869.2 | 2 | 5 | llm |  |
| SYN-01 | staged | verify_vendor_evidence | **no** | yes | yes | yes | 27153.7 | 2 | 5 | llm | action verify_vendor_evidence != use_existing_tool |
| SYN-02 | staged | route_for_review | yes | yes | yes | yes | 23613.4 | 2 | 5 | llm |  |
| SYN-03 | staged | standard_approval | yes | yes | yes | yes | 21280.4 | 2 | 5 | llm |  |
| SYN-04 | staged | request_clarification | yes | yes | yes | yes | 25912.7 | 2 | 5 | llm |  |
| SYN-05 | staged | route_for_review | yes | yes | yes | yes | 27412.5 | 2 | 5 | llm |  |
