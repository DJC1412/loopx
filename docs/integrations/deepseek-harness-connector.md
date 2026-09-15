# DeepSeek Harness Connector

Status: public-safe v0 connector for using DeepSeek Harness (`dsh`) as a
bounded agent execution host behind LoopX.

DeepSeek Harness is an open-source agent harness by DeepSeek AI. LoopX does not
replace dsh's model loop, tools, sandbox, or session log. Instead, the connector
lets LoopX govern one dsh-backed work segment at a time through the existing
LoopX Turn protocol:

```text
LoopX quota should-run
    -> loopx turn run-once
    -> loopx.dsh_goal_mode adapter (python -m loopx.dsh_goal_mode)
    -> DeepSeek Harness Python SDK / dsh runtime
    -> typed loopx_turn_result_v0
    -> independent validator
    -> LoopX writeback + quota spend
```

## What This Connector Adds

- A thin adapter, now a first-class goal-mode subpackage at
  `loopx/dsh_goal_mode/` (run with `python -m loopx.dsh_goal_mode`; the
  historical `scripts/dsh_turn_host_adapter.py` launcher still works),
  that translates
  `loopx_turn_host_request_v0` into one bounded dsh session prompt and parses
  the model's final JSON result back into `loopx_turn_result_v0`.
- A `deepseek-harness` agent type in LoopX onboarding so users can request the
  exact host instead of the generic `other-agent`.
- Optional dependency `loopx[deepseek-harness]` for the validated
  `deepseek-harness-sdk==0.1.5rc1` Python client.

## Install

Install LoopX's optional DeepSeek Harness extra:

```bash
python -m pip install 'loopx[deepseek-harness]'
```

The pin tracks the newest published dsh release channel rather than an
unreleased tag: `0.1.5rc1` for the PyPI SDK/runtime wheels and `0.1.5-rc.1` for
the npm `@deepseek-ai/dsh` `latest` tag. Upstream also publishes newer
`next`/`alpha` tags that are not the released channel. This release keeps the
Python client surface of the previously pinned `0.1.2a3` and moves the bundled
dsh runtime; LoopX selects the SDK's default `sdk` profile unless the operator
supplies an explicit cordis composition.

The DeepSeek Harness SDK spawns the bundled `dsh-jsonrpc-agent` runtime. It
uses the explicit adapter configuration plus normal provider environment
variables:

```text
DEEPSEEK_API_KEY
DEEPSEEK_BASE_URL
DSH_HOME
```

The adapter resolves its SDK home in this order: explicit `--dsh-home`, then
`DSH_HOME`, then `<workspace>/.local/.dsh-sessions`. It passes that path as the
SDK's `dsh_home` field; the SDK does not implicitly select `~/.dsh`.

Prepare a dsh `cordis.yml` when the default bundled composition is not
appropriate. See the
[DeepSeek Harness Python SDK reference](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)
for runtime selection and configuration.

## Default Host Selection

`loopx turn plan` and `loopx turn run-once` bind their default `--host` to the
operator's own provider credential rather than to the harness alone:

| Operator configuration | Default `--host` |
| --- | --- |
| `DEEPSEEK_API_KEY` is set to a non-empty value | `dsh` |
| no such credential is configured | `codex-cli` |

A machine with an operator-supplied model credential therefore runs governed
Turns against that endpoint instead of an individual CLI subscription, and a
machine without one keeps the Codex CLI host. Surrounding whitespace does not
count as a configured credential, so an exported-but-empty variable stays on
the `codex-cli` default.

`DEEPSEEK_BASE_URL` chooses the endpoint the credential is used against; on its
own it does not change the default host. An explicit `--host` always wins, and
the resolved value is what `loopx turn plan` reports back. `loopx turn run-once`
accepts the `codex-cli`, `dsh`, and `generic-cli` hosts it ships adapters for;
`loopx turn plan` additionally accepts the planning-only `claude-code` host.

## Managed Executor Readback

Both `loopx turn plan` and `loopx turn run-once` report a
`managed_executor` block, so a caller reads the planned executor instead of
inferring it from a host id:

```json
{
  "schema_version": "managed_executor_binding_v0",
  "executor": "dsh",
  "executor_kind": "managed",
  "credential_env": "DEEPSEEK_API_KEY",
  "endpoint_env": "DEEPSEEK_BASE_URL",
  "operator_credential_bound": true,
  "available": true,
  "unavailable_reason": null
}
```

`executor_kind` names where the Turn's model work is billed and bounded:
`managed` for a host bound to an operator credential, `individual` for a host
that runs on one person's own CLI login, and `generic` for a caller-supplied
adapter command. `operator_credential_bound` is the narrower claim: it is `true`
only when the operator credential or an explicit `--dsh-runner` is configured,
so an explicitly selected `dsh` host that would fall back to whatever the dsh
home configures reports `executor_kind: managed` with
`operator_credential_bound: false` instead of claiming a credential it does not
hold. `available` is `false` only when LoopX can prove the planned host cannot
launch here; it is `null` for executors this projection does not probe rather
than an unproven claim.

The credential-resolved default is the flow an operator uses without naming a
host: `loopx turn run-once --goal-id <goal> --agent-id <agent> --project <dir>`
starts the managed `dsh` Turn when an operator credential is configured, and
stays on the individual `codex-cli` host when none is. `--host` still wins when
a caller needs another executor. `examples/loopx-turn-managed-default-flow-smoke.py`
qualifies exactly that default flow end to end against a local mock model
endpoint, including the readback and the fail-closed managed-runtime case, and
consumes no operator key or individual CLI subscription.

A `run-once --execute` whose planned host reports `available: false` fails
closed: it reports the status `unavailable` with the typed
`unavailable_reason`, invokes no host, writes no journal, and spends no quota
slot. The Turn never moves onto a different executor on its own; an operator who
wants another host names it explicitly. `--dsh-runner` records that the caller
supplied its own runner, so a hermetic test hook counts as a launchable managed
host. Without that flag the built-in host needs the DeepSeek Harness runtime
from the `loopx[deepseek-harness]` extra.

## Onboard

```bash
loopx doctor --agent-type deepseek-harness

loopx agent-onboard \
  --agent-type deepseek-harness \
  --project . \
  --goal-id <goal-id> \
  --agent-id deepseek-worker \
  --available-capability shell
```

`deepseek-harness` maps to the generic CLI agent loop and uses
`--runtime-profile generic_cli` in quota/heartbeat commands.

## Run One Governed Turn

```bash
loopx turn run-once \
  --goal-id <goal-id> \
  --agent-id deepseek-worker \
  --host generic-cli \
  --execution-mode isolated-headless \
  --project "$PWD" \
  --host-adapter-command-json '["python3", "-m", "loopx.dsh_goal_mode", "--dsh-home", "/path/to/dsh-home", "--cordis", "/path/to/cordis.yml", "--model", "deepseek-v4-flash"]' \
  --validation-command-json '["python3", "/path/to/verify-postcondition.py"]' \
  --execute
```

The adapter uses `<workspace>/.local/.dsh-sessions/` as its workspace-local
SDK home by default. Override it with `--dsh-home <path>`; the historical
`--session-root` spelling remains an adapter-command compatibility alias.
Session persistence itself is owned by the selected dsh composition and is not
implied by the home-directory name.

The headless adapter derives its local session id from a versioned digest of
`[goal_id, agent_id, todo_id]`, preserving component positions so delimiters
inside an id cannot alias another lineage. The same lineage produces the same
id on retries; when all three components are absent, the existing Turn-key
fallback is retained. These opaque ids stay out of public LoopX state.

Upgrading from the former hyphen-joined naming scheme selects a new session id
for a populated lineage. The adapter does not fall back to the ambiguous old
name, rename sessions, or delete existing session files. Any persistence or
resume behavior for the newly selected id remains owned by the dsh composition.

## Run One Governed Turn In Process (`--host dsh`)

The built-in host runs the same adapter inside the CLI process:

```bash
loopx turn run-once \
  --goal-id <goal-id> \
  --agent-id deepseek-worker \
  --host dsh \
  --execution-mode isolated-headless \
  --project "$PWD" \
  --dsh-home /path/to/dsh-home \
  --dsh-cordis /path/to/cordis.yml \
  --dsh-model deepseek-v4-flash \
  --validation-command-json '["python3", "/path/to/verify-postcondition.py"]' \
  --execute
```

Unlike the subprocess mode, provider failures reach the Turn journal as typed
`loopx_turn_host_failure_v0` kinds (including the SDK's exception-free
`RunResult.finish_reason == "error"` terminal report), so bounded same-Turn
retry stays available. This mode does not promise cross-turn dsh session
continuity or an outer wake/timer. See the adapter README for the home and
classification precedence, plus the hermetic verification smoke
(`examples/loopx-turn-dsh-builtin-host-e2e-smoke.py`).

## Boundaries

- LoopX keeps the durable goal, todo, claim, gate, quota, evidence, and
  scheduler authority.
- dsh owns model calls, tools, sandboxing, and the raw session log.
- The adapter must not publish raw transcripts, dsh JSONL sessions, credentials,
  local absolute paths, or unbounded tool output into LoopX state.
- `DeepSeekHarness.run()` returns the candidate result; it is not proof of
  completion. An independent validator is required before LoopX writeback.
- The dsh Python SDK is an optional dependency. Core LoopX remains runtime
  dependency-free.
- The adapter derives an owner-local session id, but LoopX does not project or
  validate a DSH Host Session Binding. This surface therefore does not claim a
  managed supervisor or cross-process resume guarantee.

## Hermetic Validation

The repository includes four validation paths. The first three do not require the
DeepSeek Harness SDK or a real dsh runtime:

```bash
python3 examples/dsh-turn-host-adapter-smoke.py
python3 examples/loopx-turn-dsh-e2e-smoke.py
python3 examples/loopx-turn-dsh-builtin-host-e2e-smoke.py
```

The first guards adapter translation and result shaping. The second drives the
full `loopx turn run-once -> adapter -> fake dsh -> validator -> writeback ->
quota spend -> idempotent replay` chain. The third proves the built-in host's
success path plus three bounded provider-capacity attempts, retry-budget
exhaustion without a fourth Host invocation, zero failure spend/writeback, and
provider-prose non-persistence.

The fourth uses the real `deepseek-harness-sdk` and the bundled dsh JSON-RPC
runtime. It still avoids a real model call by serving a local mock OpenAI-compatible
SSE endpoint, so it is hermetic and does not require `DEEPSEEK_API_KEY`:

```bash
python3 examples/loopx-turn-dsh-real-e2e-smoke.py --host generic-cli
python3 examples/loopx-turn-dsh-real-e2e-smoke.py --host dsh
```

The real-dsh smoke clears ambient DSH home variables and proves that both host
paths can supply an explicit SDK home, start the actual dsh runtime, run one
bounded turn through the real JSON-RPC agent loop, parse a typed JSON final
message, and complete LoopX validation/writeback/quota spend.

## Related Contracts

- [DeepSeek Harness control-plane adapter](deepseek-harness-control-plane-adapter.md)
- [Runtime connector catalog](runtime-connector-catalog.md)
- [LoopX Turn v0](../reference/protocols/loopx-turn-v0.md)
- [Host integration surface v0](../reference/protocols/host-integration-surface-v0.md)
- [Embed LoopX In Your Agent Runner](../guides/custom-agent-runner-integration.md)
