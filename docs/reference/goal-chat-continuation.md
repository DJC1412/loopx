# Continue work in the existing Goal Chat

The existing frontend **Goal → Chat** is the conversational coordinator entry.
A coordinator is a responsibility: the conversation can carry it, or the owner
can assign peer task coordination to a registered Agent. The local steward
handles cross-Goal intake and owner attention; the project conversation keeps
its Goal context, work discussion and returned results. These roles reuse the
same scoped collaboration and canonical acceptance boundaries. A role does not
select an executor, create a worker, transfer a lease or start a continuous loop.

## Explicit Codex continuation

For a **managed Codex Goal Chat**, enter the following in the existing message
box. This opt-in uses the native Codex Goal in the conversation's existing
upstream thread; ordinary messages retain the normal single-turn behavior.
The host must support the experimental app-server `thread/goal/*` APIs.

```text
/goal start --tokens 100000 Inspect the project evidence, compare the revisions, and report the verified conclusions here.
/goal status
/goal resume --tokens 200000
```

- `start` requires a nonempty objective of at most 3000 characters. An unfinished
  native Goal must be resumed; `start` cannot silently replace it.
- `resume` keeps the native objective and accumulated usage. The number is the
  **total native token allowance**, including input context and previous usage,
  and must exceed usage already observed. It is not an additional allowance or
  a strict cost ceiling: an in-flight model request can overshoot it.
- `status` reads the current native state without a model turn. `/goal` shows
  command help. State notifications trigger authoritative readback, so old
  notifications on recovery cannot report a stale stop as the current state.
- To pause, open the current run → **Details and actions → More run actions →
  Interrupt this run**. This interrupts the current native turn and pauses the
  native Goal. Resume in the same conversation. Sending an ordinary follow-up
  after pausing does not activate the native Goal again.
- Each activation stays within the Chat service's configured hard timeout.
  A timeout pauses work; explicit resume continues the same thread. Browser
  reconnect observes the existing local turn. Service recovery pauses the
  native Goal before accepting more work; it never silently starts a replacement
  thread. Stop or close the Chat service before rolling back to an older build.

The native driver supplies the continuation; there is no business-phase script
and no second LoopX scheduler. Multiple native turns remain one observed Chat
run, with output in the original conversation. A native completion that arrives
before its final answer does not truncate that answer. Budget or usage limits
interrupt in-flight work as soon as the host reports them.

## Scope and acceptance

Continuation retains the Goal Chat's **read-only sandbox**, existing Codex home,
model/provider and approval policy. It does not use the steward's trusted-owner
profile. Attached hosts, other executors, the steward channel and execution-task
channels reject this command. Use an ordinary message to provide image context
before starting continuation.

`complete`, `blocked`, `paused`, `budgetLimited` and `usageLimited` are native
host observations. They do not complete a LoopX Goal/Todo, satisfy an acceptance
validator, deliver a peer result or grant mutation authority. The native answer
cannot supply executable proposals or handoff receipts. Registered workers keep
their independent profiles, work commitments and execution/acceptance bindings.

This stage qualifies read-only conversation continuity. Scoped conversational
handoff is the separate [#4696](https://github.com/huangruiteng/loopx/pull/4696)
companion. Automatically dispatching and accepting a heterogeneous team across
native continuation cycles remains the next integration boundary; this feature
does not claim that qualification, Ark/DSH driver parity or unattended daemon
operation. The [session RFC](../architecture/rfcs/agent-session-execution-modes-v0.md)
and [overall roadmap](../architecture/rfcs/loopx-overall-roadmap-v0.md) retain those
acceptance requirements.

## 中文使用说明

基线入口就是现有的 **Goal → 对话**。对话可以承担项目协调职责，也可以把
peer 任务协调职责明确分配给已注册 Agent；跨项目管家继续负责全局接待与需要
所有者处理的事项。职责、执行身份、运行驱动和验收权限分别由原有边界管理。

在使用 managed Codex 的 Goal 对话中直接输入：

```text
/goal start --tokens 100000 阅读本项目资料，比较修订前后的变化，检查计算并把报告返回当前对话。
/goal status
/goal resume --tokens 200000
```

`start` 的目标最多 3000 字符；已有未完成的原生 Goal 需要用 `resume` 继续。
数字是包含输入上下文与历史消耗的原生总 token 额度，续跑时不会清零；已发出的
模型请求可能超额。`status` 不调用模型，`/goal` 显示帮助。

暂停沿用当前运行详情中的 **详情与操作 → 更多运行操作 → 中断本次运行**。
随后在原对话 `resume`，或发送普通问题。普通消息不会再次启用持续推进。
每次激活仍受 Chat 服务的硬超时约束；浏览器重连继续观察当前运行，服务恢复
先暂停原生 Goal，再接受新工作。回滚旧版本前先停止本次运行或关闭 Chat 服务。

此功能保留 Goal 对话的只读沙箱和原有模型/provider，不继承管家的 trusted-owner
权限。原生 Goal 完成、暂停、阻塞与限额状态均会明确显示；它们不是 LoopX 的
任务验收结论。本阶段打通同一对话的持续分析与恢复，跨多个持续回合自动驱动
混合 Agent 团队并收齐独立验收结果，仍需要与共享协作链路继续集成。
