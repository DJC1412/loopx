# Canonical follow-up capture

`todo capture-followups` records a bounded batch of **unclaimed Agent Todos**.
It does not complete another Todo, acquire a lease, authorize an executor, or
perform the captured work. The built-in Todo planner owns selection and metadata;
coordination owns the canonical transaction. No new capability or provider is
introduced.

## Use and recover

On an already promoted Goal:

```bash
loopx --format json todo capture-followups --goal-id example \
  --capture-operation-id followups-review-1 \
  --follow-up 'Validate the recovery boundary' \
  --follow-up 'Document the operator recovery command' \
  --evidence 'validation://reviewed-capture' \
  --continuation-policy same_agent_non_delivery \
  --required-capability code_review --dry-run
```

Remove `--dry-run` to commit. Reuse the same operation id **and intent** after a
lost response; changing the evidence, metadata, order or follow-ups under that
id is rejected. Omit the id for a fresh operation on every invocation. Preview
writes no business receipt, so its id can be used for the subsequent commit.
An explicit id requires canonical authority; legacy Markdown cannot promise a
durable command receipt and rejects that option before writing.

Read back through `loopx --format json todo list --goal-id example`. File and
SQLite use the existing local selector. PostgreSQL uses the same transaction
through the existing service-owned store factory, with its own authentication
and tenant admission; this command does not introduce standalone PostgreSQL CLI
configuration or silently fall back when a selected provider is unavailable.

## Selection and atomicity

One TypeScript plan processes the whole request in order:

1. Empty text is skipped.
2. Existing public-safe boundary heuristics reject local paths, credential-like
   literals and internal-only markers. Unsafe **evidence rejects the batch**;
   an unsafe follow-up is reported as skipped. These are bounded heuristics,
   not comprehensive secret detection or a grant to publish captured text.
3. Duplicate full text is skipped after Python-compatible whitespace compaction.
   All Agent Todos in the active section count, including done and deferred
   records. User and archived records do not suppress capture.
4. At most two new Todos are accepted; skipped items do not consume that limit.

The old 500-character **display** limit no longer defines capture identity.
Distinct long texts remain distinct, and rereading the legacy source uses an
explicit lossless decoder mode. Machine projection also validates through lossless source/metadata codecs rather
than status summaries; long records can be delivered instead of remaining pending.
Missing native priority/title annotations are derived for display, while explicit
contradictions still fail parity. Other display callers retain their limits.
`--continuation-policy`, previously accepted but dropped by the CLI, now reaches
both writers. Invalid metadata or a malformed tail rejects the entire request;
invalid requirements are not silently removed. This applies even to a batch
whose texts would all be duplicates. Optional routing metadata is returned
when supplied, rather than as an unrelated catalog of empty fields.

The legacy adapter holds its existing lock, renders only the accepted plan,
and writes once through shadow capture. It no longer owns regex classification,
selection, cap, duplicate identity or per-item add decisions. The canonical
adapter reads the full head, shares native Todo creation admission/materialization,
then commits all accepted rows and one receipt under one provider CAS. A stale
CAS cannot leave the first row committed without the second. Original Todo,
lease and standing-decision records remain untouched.

A no-op batch also seals a receipt while leaving the domain head unchanged;
its provider revision can advance even though `changed=false`. After later
archive/edit operations, retrying that id still returns the original no-op.
On replay, `changed=false` describes this invocation, while `recorded_count`,
`items[].added` and `original_receipt` describe the historical batch. A receipt
never grants current execution authority.

## Display delivery and boundaries

Markdown is delivered separately through the existing committed-authority outbox.
A display failure returns successful business state with
`projection_delivery=pending`; it does not roll the batch back. Retry the same
operation, or use the existing `todo project-markdown` recovery command for the
current provider revision. Delivery renders the **latest head**, not a stale
receipt snapshot. Missing Markdown does not prevent canonical capture or preview.
A missing/unavailable canonical provider also blocks preview; it must not invent
success from legacy Markdown.

The affected entry point is the CLI/Python capture API and its shared TS runtime.
There is no dedicated capture-followups frontend or Lark editor; existing Todo
readers consume its ordinary unclaimed records and the normal projection outbox.
No UI setting or new default is added. Reverting this code requires keeping
promoted Goals fenced and withholding this command until its transaction is
restored; do not re-enable the old Markdown writer as a rollback shortcut.

## Validation and roadmap checkpoint

Native conformance runs over File, SQLite, NoKV and real isolated PostgreSQL,
including complete synthetic graphs, legacy/native records, no-op/replay,
competing CAS, invalid input, source changes and lost acknowledgements. Python
coverage exercises the public CLI on real File/SQLite, missing display,
projection failure/recovery and provider failure without legacy fallback.

The read-only snapshot rehearsal compares legacy, File, SQLite and the
PostgreSQL service runtime. Supply a disposable PostgreSQL server explicitly:

```bash
uv run --extra test python examples/control_plane/authority-followup-capture-rehearsal.py \
  --registry <registry> --goal-id <goal> --execute-isolated-postgresql \
  --private-diagnostics <ignored-diagnostic-file>
```

`LOOPX_TEST_POSTGRES_URL` selects that isolated server. All effects target temporary
copies and a disposable tenant. Reports contain bounded counts/digests, and the
source is checked unchanged afterward. This is a command qualification, not a
whole-Goal promotion or a long-duration soak.

This closes capture-followups within shared-authority **L2** and TS **T1**.
It does not close the separate **L7 shadow-capture continuity** package. Remaining
public mutation/effect admission, consumers, D1/D2/D3, whole-Goal rollback,
new-Goal defaults and final legacy-writer retirement retain their existing
owners and acceptance gates. Consult the [shared-authority program](../architecture/rfcs/shared-goal-authority-state-provider-v0.md)
and [TS migration roadmap](../architecture/rfcs/typescript-control-plane-migration-v0.md).
