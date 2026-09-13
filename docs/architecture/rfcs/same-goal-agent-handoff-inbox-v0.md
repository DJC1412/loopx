# RFC: Same-Goal Agent Handoff Inbox v0 / 同 Goal Agent 交接收件箱 v0

| Field / 字段 | Value / 值 |
|---|---|
| Status / 状态 | Implemented / 已实现 |
| Date / 日期 | 2026-09-13 |
| Core owner / Core 责任 | typed dispatch, durable receipt, Turn-start projection / 类型化派发、持久回执、Turn-start 投影 |
| Agent owner / Agent 责任 | canonical Todo claim, bounded execution and result writeback / 规范 Todo 认领、有界执行与结果回写 |

## Decision / 决策

An `independent_handoff` Todo that excludes the current Agent is not a quiet
wait. When a registered same-Goal peer remains eligible, the coordinator
deterministically dispatches the first candidate to that peer. The handoff is
stored under the existing manager-context inbox and projected by the same
`manager.context_inbox` Turn-start hook used by quota heartbeats and managed
`turn run-once`. It does not introduce a second message queue or a second UI
source of truth.

如果一个 `independent_handoff` Todo 排除了当前 Agent，它不能被当作静默等待。
只要同一 Goal 内仍有符合条件的已注册 peer，协调者就把第一个候选确定性地派发给
该 peer。交接记录复用现有 manager-context 收件箱，并由同一个
`manager.context_inbox` Turn-start hook 投影给 quota heartbeat 与托管
`turn run-once`；不新增第二套消息队列或 UI 真相源。

Dispatch is admitted only after live readback proves that the Todo comes from
canonical authority, remains open and unclaimed, uses `independent_handoff`,
excludes the sender, and does not exclude the recipient. A message-delivery
receipt is not execution authority. The recipient must run the exact projected
`todo claim` command and then `manager-inbox acknowledge-handoff`. The latter
re-reads canonical Todo state before changing the durable receipt from
`dispatched` to `claimed`.

派发前必须实时读回并证明：Todo 来自规范 authority、仍为 open 且未被认领、
使用 `independent_handoff`、排除发送者且不排除接收者。消息送达回执不等于执行
权限。接收者必须先运行投影出的精确 `todo claim` 命令，再运行
`manager-inbox acknowledge-handoff`；后者会再次读取规范 Todo 状态，验证成功后
才把持久回执从 `dispatched` 更新为 `claimed`。

## Identity, isolation and replay / 身份、隔离与重放

- The dispatch identity binds Goal, Todo, sender and recipient. Canonical state
  is read immediately before the first dispatch; later retries replay the same
  receipt instead of creating a new identity for unrelated provider revisions.
  Replaying the same decision returns the existing receipt.
- Inbox paths are keyed by both `goal_id` and `agent_id`; a same-named Agent in
  another Goal cannot read or acknowledge the handoff.
- The recipient is selected from the intersection of the candidate's eligible
  peer set and the Goal's registered Agent set, excluding the sender.
- Private inbox entries and receipts are local mode `0600`. Public quota/Turn
  output contains only bounded identifiers and receipt state, never inbox
  content or provider payloads.
- A handoff grants no repository, provider, trading, payment, publishing or
  other protected-operation authority. Existing Todo capability and write-scope
  gates continue to apply after claim.

- 派发身份绑定 Goal、Todo、发送者与接收者；首次派发前立即读取规范状态，后续
  重试复用同一回执，不会因无关的 provider revision 变化生成新身份。
- 收件箱路径同时按 `goal_id` 与 `agent_id` 分区；其他 Goal 下的同名 Agent
  无法读取或确认该交接。
- 接收者从“候选允许的 peer”与“该 Goal 已注册 Agent”的交集中确定，并排除
  发送者。
- 私有收件箱条目和回执在本地使用 `0600` 权限；公开 quota/Turn 输出只包含
  有界标识与回执状态，不包含收件箱正文或 provider payload。
- 交接不授予仓库、provider、交易、支付、发布或其他受保护操作权限；认领后仍
  受 Todo 原有 capability 与写范围 gate 约束。

When no eligible peer exists, Core emits the non-quiet typed state
`handoff_dispatch_state=no_eligible_peer` and forbids spend until a peer is
registered. It never lets the excluded coordinator execute the Todo.

当不存在符合条件的 peer 时，Core 发出非静默类型化状态
`handoff_dispatch_state=no_eligible_peer`，并在注册合适 peer 前禁止 spend；
它不会让被排除的协调 Agent 执行该 Todo。

If the Todo is claimed by another peer between dispatch and recipient
acknowledgement, acknowledgement writes a terminal `claim_conflict` receipt
with the observed claimant. It does not overwrite the canonical claim or leave
the stale inbox item pending forever.

如果 Todo 在派发和接收者确认之间被另一个 peer 抢先认领，确认操作会写入终态
`claim_conflict` 回执并记录实际 claimant；它不会覆盖规范认领，也不会让过期
收件箱条目永久悬挂。

## Product surfaces and acceptance / 产品入口与验收

- CLI/quota and managed Turn share the same Turn-start hook and dispatch code.
- Lark remains an adapter over the same inbox/read/receipt model; it does not
  own handoff state.
- No frontend control is added in v0 because this is an internal Agent routing
  transition, not user-configurable state. Existing frontend Todo and Turn
  projections continue to consume the bounded shared contract capsule.
- Acceptance covers deterministic replay, Goal/Agent isolation, canonical
  claim-before-ack, private file modes, the no-peer frontier, and Turn-envelope
  receipt projection.

- CLI/quota 与托管 Turn 共享同一 Turn-start hook 和派发实现。
- Lark 仍只是同一 inbox/read/receipt 模型的适配器，不持有独立交接状态。
- v0 不增加前端控件，因为这是内部 Agent 路由转换，不是用户可配置状态；现有
  前端 Todo 与 Turn 投影继续消费有界的共享 contract capsule。
- 验收覆盖确定性重放、Goal/Agent 隔离、先规范认领再确认、私有文件权限、无
  peer frontier，以及 Turn envelope 回执投影。
