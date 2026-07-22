# ROUTING.md — context-loading routing (非 surface 路由)

conditional paths are indexes; read only when their trigger matches.

```yaml
load_if:
  roundtable:                              # 触发即用 roundtable skill（含通信纪律）
    when:
      - inbound_roundtable_message         # [FROM→TO kind id=...] 到达
      - peer_agent_coordination            # Claude / Codex / Hermes 互为 peer
      - rt_say_or_ack_or_refresh_or_resolve
      - handoff_delivery
      - surface_or_routing_debug
    skill: roundtable
    read:
      - .roundtable/agents.yaml

  brief:
    when:
      - scope_or_requirements_question
      - project_goal_question
    read:
      - BRIEF.md

  decisions:
    when:
      - prior_decision_question
      - workflow_policy_question
      - historical_context_needed
    read:
      - decision.md
```
