# ROUTING.md — context-loading routing（非消息投递路由）

conditional paths are indexes; read only when their trigger matches.

```yaml
load_if:
  pneu:                                    # 触发即用 pneu skill（含通信纪律）
    when:
      - inbound_pneu_message               # [FROM→TO kind id=...] 到达
      - peer_agent_coordination            # Claude / Codex / Hermes 互为 peer
      - rt_say_or_ack_or_refresh_or_resolve
      - handoff_delivery
      - wake_or_delivery_debug
    skill: pneu
    read:
      - .roundtable/agents.yaml

  principles:
    when:
      - design_or_architecture_question
      - harness_adaptation_proposal
      - support_claim_question
      - writing_a_dispatch_brief
    read:
      - PRINCIPLES.md

  brief:
    when:
      - scope_or_requirements_question
      - project_goal_question
    read:
      - BRIEF.md
      - BACKLOG.md                         # open work index

  decisions:
    when:
      - prior_decision_question
      - workflow_policy_question
      - historical_context_needed
    read:
      - decision.md
```
