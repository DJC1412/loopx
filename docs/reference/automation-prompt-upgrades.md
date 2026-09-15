# Managed Codex heartbeat prompt upgrades

## Product contract

Default execution remains `heartbeat-prompt --thin`. An adopted automation
stores a small, stable bootstrap instead of a copy of the current execution
rules. At **every wake**, it requests the complete JSON thin prompt from the
installed LoopX, with an explicit registry, Goal, agent and host capability
binding, then follows that prompt. After a normal LoopX upgrade, the next wake
reads the new rules; no per-release Codex database rewrite is needed. An already
running wake keeps its current instructions. This is model-mediated loading,
not a guarantee that a model will obey every instruction.

The bootstrap is a transport wrapper, not a new execution mode. It does not
default to full/compact prompts, contain a parallel work policy, or create
another scheduler. Failure to load a complete successful response stops work
and spending; it must not fall back to remembered rules. Project watches and
business policy belong in LoopX state, not in this bootstrap.

The v2 wrapper distinguishes continued work, notifications and real waits:
one operation does not end the work; notification silence is not execution
silence; waits follow the live scheduler contract instead of unchanged polling.
Local entrypoint errors may be repaired within existing authority, but an
unavailable contract still forbids delivery and spending. This is not permission
to bypass a gate, retry indefinitely, or disable a healthy automation.

For a one-agent trial, pass `--cli-bin loopx-canary` to preview and apply. The
bootstrap and the thin prompt's generated commands both use that executable;
other automations continue to use their existing runtime. Do not promote the
canary as the global default merely to test one task.

The owner is the existing heartbeat/upgrade boundary; there is no new optional
capability or extension provider. SQLite/TOML handling is a local host adapter,
not Todo, quota or scheduler authority.

## Automatic upgrade and manual adoption

`loopx update apply` now captures an owner-only snapshot **before** replacing
the runtime and invokes `automation-prompts sync-installed` in the **new**
runtime afterward. Exact recognized managed bootstraps and byte-identical
prompts reproduced by the old installed generator may migrate automatically.
An automation name, matching prose or Goal id alone never authorizes adoption.
Custom instructions remain `review_required`; a canary executable, different
registry/home or changed preview is not silently retargeted. Binary-install
success and prompt-migration success are reported separately. `upgrade_complete`
is true only when runtime qualification and prompt reconciliation both succeed;
the existing `ok` field retains its runtime-result meaning. A successful install
and core doctor still run prompt reconciliation when an optional extension
fails its doctor, without clearing that extension failure or its repair action.
Failed installation or core doctor never starts prompt writes.

Upgrading from a CLI that predates this hook cannot retroactively capture its
old template evidence. After installation, run the new `automation-prompts plan`
and review/adopt the selected tasks through the App. Never interpret absence of
a migration report as proof of migration, or loosen exact-template ownership
to make a historical custom prompt appear automatically eligible.

Archive updates pinned with `--ref <full-commit-SHA>` download that exact archive
without a GitHub commit-API lookup. Symbolic refs still require resolution to a
full hexadecimal SHA. If the public API fails, the installer can reuse existing
`gh` authentication for the same GitHub repository/ref under a bounded timeout;
it neither logs in nor selects another ref. If both routes fail, retry with an
independently verified full SHA. Downloads have bounded timeouts and retries.

**Exact managed v1 wrappers upgrade to v2 automatically through this path;
they do not require per-task approval.** `automation-prompts plan` is only a
read-only preview, not the upgrade executor. Do not infer a manual-only policy
from its `adoption_required` status. Custom or inconsistent entries still need
review; automatic prompt migration never grants scheduler or thread authority.

## Live turn observation

Install-time reconciliation is one-shot: a pending adoption that nobody applies
would otherwise never reappear, and a repaired or resumed lane can keep running
yesterday's frozen body. `quota should-run` therefore reports the caller's own
installed automation in `scheduler_hint.app_automation.prompt_binding` (schema
`codex_app_automation_prompt_binding_v0`: `current`, `adoption_required`,
`blocked`, `ambiguous`, `absent`, or `unavailable`). A stale entry adds
`host_action=adopt_managed_bootstrap`, its no-spend policy, both prompt digests,
and the same reviewed prompt-only `automation_update` request that update-time
reconciliation returns, so the obligation survives in the live contract instead
of a completed report.

The observation is bounded, read-only, and fail-open: an unreadable or
disagreeing store reports a status and never fails the turn, and a lane without
an installed automation projects no field at all. Several installed automations
can claim one Goal/agent, so the observer follows the one bound to the current
thread and classifies only a body whose TOML and App records agree; unconfirmed
look-alikes are named only when nothing else is confirmable. A recognized loader
is always compared with its own binding, because a turn must never retarget another Codex
home, registry, runtime root, or CLI binary; only an unrecognized body is
reviewed against the caller's registry. Adoption stays explicit and
non-blocking: it is not delivery permission, not a scheduler authority, and a
deliberate owner-pinned body is reviewed rather than overwritten.

On the qualified macOS heartbeat schema, direct migration requires the App
closed. The adapter holds a SQLite writer transaction through TOML delivery,
compares the entire previewed manifest, preserves every non-prompt field, and
reads both stores back. Keep the App closed through readback, then restart it.
A running App caches automation state and can overwrite both stores after a
disk-only update; a transaction or immediate readback cannot invalidate that
cache. Running hosts must use the native `automation_update` API. The CLI reports
that required host action instead of claiming the upgrade is complete.

This is a local storage compatibility adapter, not an official Codex API.
Uncoordinated TOML writes cannot participate in the SQLite transaction;
detected changes/crashes retain the private journal and require reconciliation.

Unsupported platforms/schemas, custom prompts and conflicts are reported per
task. Where an eligible task can instead use the native App writer, the report
includes a complete `automation_update` request with preserved fields and a
fresh-view/hash precondition. A CLI cannot invoke an in-App tool itself. Do not
blindly replay a request after a user edit. A failed runtime installation never
starts prompt migration. A pending report retains the private pre-update plan
so a selected task can be retried, without scanning another Codex home:

```sh
loopx automation-prompts sync-installed --plan-file ./private-before.json --automation-id TASK_ID --execute
```

The existing `plan` command remains read-only and explicit `apply` remains an
offline, reviewed path; it does not automatically adopt custom instructions.

```sh
loopx --format json automation-prompts plan --plan-file ./private-prompt-plan.json
```

The plan contains current and proposed prompts and is **private local data**.
Do not commit, upload, or paste it into public issues. The file is owner-only.
Use repeatable `--automation-id` selectors to narrow the plan. Discovery uses
registered Goal/agent identities only to propose candidates; it never grants
permission to overwrite them. Unrelated, ambiguous and inconsistent host
records are not adopted. Review custom instructions before replacement: move
durable project policy to its LoopX owner, or leave that task unadopted. There
is no automatic prose merge or substring-based permission to replace a prompt.

While the Codex App is running, prefer its `automation_update` interface:
view each selected task, verify its current prompt against the preview, update
only the prompt to `desired_prompt` while preserving every other field, then
read it back. Do not recreate the task or rebind its thread. A single user
request can authorize this reviewed batch; the plan itself does not execute it.

When that API is unavailable, the qualified macOS offline fallback is:

```sh
# Review the plan, then close the Codex App first.
loopx automation-prompts apply --plan-file ./private-prompt-plan.json --offline --execute
```

The same registry/runtime-root and Codex home must be used for preview and
apply. `--codex-home` explicitly selects one home; the command never searches
other homes or copies sessions between them. No new automation is created.
Schedule, pause state, model, notification preferences, thread binding and run
history are preserved. Each selected task commits independently; the command
reports partial failure instead of claiming the whole batch succeeded.

Only existing heartbeat records with matching SQLite/TOML identity, prompt,
status, schedule and thread binding are eligible. Unknown schemas and stale previews fail
closed. The fallback checks that the macOS App is closed; keep it closed until
readback completes. Restart afterward. This adapter targets the observed local
schema, **not an official stable Codex storage API**; no Windows/cloud support
is claimed. Use the native API if the host changes its storage contract.

A legacy TOML heartbeat label with a SQLite `cron` row is not enough to prove
thread ownership. In particular, a missing database thread binding must not be
filled from TOML by a prompt-only migration. Such records require App-mediated
reconciliation first; the offline adapter reports that action explicitly and
does not convert scheduler kind, infer a thread, or offer an executable upgrade.

## Recovery and rollback

The adapter stores a private per-task journal before writing. SQLite commit
and TOML replacement are not one transaction: a crash can leave a mirror pending,
including a new TOML prompt with a rolled-back SQLite prompt.
While the App remains closed, recover that exact task:

```sh
loopx automation-prompts recover --automation-id TASK_ID --offline --execute
# Or restore the previous prompt:
loopx automation-prompts rollback --automation-id TASK_ID --offline --execute
```

Recovery is idempotent. It refuses to overwrite later prompt/metadata edits.
The journal remains under the selected Codex home's `loopx-automation-backups`
directory; do not publish it. Rollback restores only the recorded prompt,
never an entire historical database or session table. Native API migrations
should retain the reviewed previous prompt privately and use that same API
for rollback.

Disable automatic rule adoption by replacing the bootstrap with an explicitly
pinned prompt using the App, or pause the task there. LoopX runtime rollback
also changes the rules loaded on the next wake. Exact v1 wrappers remain
recognized as runtime-loaded thin prompts; `automation-prompts plan` proposes
v2 without writing, while the update-time exact-owned path can migrate them.
Customized wrappers are not recognized merely from their header.

## Goal host loaders

`heartbeat-prompt --bootstrap` uses the same shell loader renderer as automation.
New Codex App/SSH, Codex CLI/IDE and managed-agent activations store this loader;
it preserves explicit caller policy but re-resolves registry state on each load.
Its inner command omits `--bootstrap`, preventing recursion. Persisting a fixed
turn-instance id is rejected. Existing saved native Goal objectives are not
rewritten through SQLite.

Claude Code stores a bound MCP loader: `host_prompt` returns the current inner
rules for its existing Goal and agent. Restart an existing MCP server after a
runtime upgrade; already imported Python code does not hot-reload. TraeX retains
its direct Goal projection when capabilities require a separate host surface;
the generic loader must not move those declarations into prompt text.

## 中文摘要

默认仍是 thin。一次迁移后，automation 每次唤醒读取已安装 LoopX 的最新
thin 指令；之后升级 LoopX 即可让下一轮采用新版规则，无需逐版本改 SQLite。
正在运行的轮次不热切换。启动器不是另一套执行规则，也不增加权限。

升级主流程在替换 runtime 前留存旧模板证据，升级后由新 runtime 生成并迁移。
仅完整匹配的托管指令可自动更新；自定义内容不猜测合并、不自动删除。macOS
已匹配的 heartbeat 存储支持 App 运行中直接写入，仅改 prompt，持有数据库写锁
直到 TOML 交付和读回完成。两种存储并非一个原子事务；冲突或异常保留私有日志，
不伪报成功。`upgrade_complete` 同时覆盖 runtime 与 prompt；可选扩展检查失败
不阻止已经通过安装和核心 doctor 的 runtime 继续迁移 prompt，扩展故障仍保留。
首次从不含迁移钩子的旧 CLI 升级，应再用新版 plan 审阅并采用旧任务，不能把
缺少迁移报告当作已迁移。完整 SHA 下载不依赖提交查询；分支查询失败可复用已有
gh 登录，仍失败则明确要求已核验 SHA，不切换分支或静默覆盖自定义指令。
日程、暂停状态、模型、线程、通知偏好和历史均不迁移。
不支持的存储仍需原生 API；运行中的本轮不热切换。普通测试不消耗模型 token，
真实模型发布资格仍需独立评测，不能由迁移成功推断。

安装期对账只报告一次，因此 `quota should-run` 每轮都把本轮 lane 已安装的
automation 观测投影为 `scheduler_hint.app_automation.prompt_binding`：状态为
`current`/`adoption_required`/`blocked`/`ambiguous`/`absent`/`unavailable`。
非当前 loader 时附带 `host_action=adopt_managed_bootstrap`、no-spend 策略、
两个 prompt 摘要，以及与升级期完全相同的、仅改 prompt 的
`automation_update` 请求，使采纳义务留在实时契约里，而不只存在于一次性报告。
该观测只读、有界、失败即降级，未安装 automation 的 lane 不投影该字段；已识别
的 loader 一律按自身绑定比对，turn 不会改标到其他 home、registry、runtime root
或 CLI。同一 Goal/agent 可能装了多个 automation，因此观测跟随当前 thread 绑定的
那一个，并且只对 TOML 与 App 记录一致的 body 分类；未确认的同名记录仅在没有可确认
body 时才列出。采纳仍需显式执行，既不授予交付权限也不接管调度。
