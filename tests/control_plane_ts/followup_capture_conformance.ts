/** Batch semantics are identical over every real AuthorityStore implementation. */
import assert from "node:assert/strict";
import test from "node:test";
import type {JsonObject} from "../../loopx/control_plane/effect_program.ts";
import type {AuthorityStore} from "../../loopx/control_plane/coordination/authority_store.ts";
import {executeCoordinationFollowupCapture as capture} from "../../loopx/control_plane/coordination/todo_followup_capture.ts";
import {prepareCoordinationProjectionCommit} from "../../loopx/control_plane/coordination/coordination_projection.ts";
import type {AuthorityStoreConformanceFactory} from "./authority_store_conformance.ts";
import {productionScaleCoordinationFixture} from "./production_scale_coordination_fixture.ts";
import {authorityProjectionFixture} from "./authority_projection_fixture.ts";

const request = {goal_id: "capture-goal", operation_id: "capture-batch", dry_run: false,
  now: new Date("2026-09-07T07:00:00Z"), intent: {followups: ["First new task", "Second new task"],
    evidence: "validation://batch", metadata: {continuation_policy: "same_agent_non_delivery",
      required_capabilities: ["Code-Review"], required_write_scopes: ["src/**"]}}};
async function head(store: AuthorityStore) {
  const result = await store.loadAuthority();
  assert.equal(result.status, "loaded");
  if (result.status !== "loaded") throw new Error("missing fixture");
  return result;
}
export function registerFollowupCaptureConformance(provider: string, factory: AuthorityStoreConformanceFactory) {
  for (const schema of ["native", "legacy"] as const) test(`${provider}: capture batch preserves complete ${schema} state and seals no-op history`, async t => {
    const {store} = await factory(t);
    const fixture = productionScaleCoordinationFixture(request.goal_id, schema);
    const initial = fixture.projection;
    const old = initial.todos as JsonObject[];
    const done = old.find(row => row.role === "agent" && row.status === "done" && row.archive_state === "active")!;
    assert.ok(done);
    const prefix = "Long identity ".repeat(45);
    const input = {...request, intent: {...request.intent,
      followups: [String(done.text), prefix + "A", prefix + "B", "Third new task"]}};
    assert.equal((await store.commitAuthority({operation_id: "seed", expected_provider_revision: null,
      next_projection: initial, events: [], receipts: []})).status, "applied");
    const before = await head(store);
    const preview = await capture(store, {...input, dry_run: true});
    assert.equal(preview.status, "planned");
    assert.equal(preview.recorded_count, 2);
    assert.deepEqual(await head(store), before);
    assert.equal((await store.readReceipt(input.operation_id)).status, "missing");
    const result = await capture(store, input);
    assert.equal(result.status, "applied", JSON.stringify(result));
    assert.deepEqual((result.items as JsonObject[]).map(i => i.skipped_reason), ["duplicate", null, null, "max_items_exceeded"]);
    const after = await head(store);
    const added = (after.head.todos as JsonObject[]).filter(row => !old.some(o => o.todo_id === row.todo_id));
    assert.equal(added.length, 2);
    assert.deepEqual((after.head.todos as JsonObject[]).filter(row => old.some(o => o.todo_id === row.todo_id)), old);
    assert.deepEqual(after.head.leases, initial.leases);
    for (const row of added) {
      assert.equal(row.claimed_by, undefined);
      assert.equal(row.continuation_policy, "same_agent_non_delivery");
      assert.deepEqual(row.required_capabilities, ["code_review"]);
    }
    const noOp = {...input, operation_id: "all-duplicates", intent: {...input.intent, followups: [prefix + "A", prefix + "B"]}};
    const noChange = await capture(store, noOp);
    assert.equal(noChange.status, "no_change", JSON.stringify(noChange));
    assert.equal(noChange.changed, false);
    assert.equal(noChange.recorded_count, 0);
    const sealed = await head(store);
    assert.deepEqual(sealed.head, after.head, "receipt-only write preserves domain state");
    await store.commitAuthority(prepareCoordinationProjectionCommit({goal_id: request.goal_id, operation_id: "retire-captured",
      expected_provider_revision: sealed.provider_revision, projection: sealed.head,
      mutations: added.map(todo => ({kind: "todo_upsert" as const, todo: {...todo, archive_state: "archive"}}))}));
    const later = await head(store);
    for (const original of [input, noOp]) {
      const replay = await capture(store, {...original, now: new Date("2030-01-01")}, async () => { throw new Error("replay must precede admission"); });
      assert.equal(replay.status, "replayed");
      assert.equal(replay.changed, false);
      assert.deepEqual(await head(store), later);
    }
    const mismatch = await capture(store, {...input, intent: {...input.intent, evidence: "validation://different"}});
    assert.equal(mismatch.reason_code, "coordination_operation_identity_mismatch");
  });

  test(`${provider}: capture rejects invalid whole batches and stale CAS without partial rows`, async t => {
    const {store, contender} = await factory(t);
    const projection = authorityProjectionFixture(request.goal_id, []);
    await store.commitAuthority({operation_id: "seed", expected_provider_revision: null, next_projection: projection, events: [], receipts: []});
    const before = await head(store);
    for (const intent of [
      {...request.intent, followups: ["Valid first item", 42]},
      {...request.intent, metadata: {required_capabilities: ["valid", "bad/token"]}},
      {...request.intent, metadata: {claimed_by: "agent-a"}},
      {...request.intent, metadata: {task_class: "user_gate"}},
    ]) {
      assert.equal((await capture(store, {...request, intent})).status, "failed");
      assert.deepEqual(await head(store), before);
      assert.equal((await store.readReceipt(request.operation_id)).status, "missing");
    }
    // The competitor commits between our validated read and CAS. It is a real
    // provider transaction, not a fake conflict or a pair of partial inserts.
    let raced = false;
    const racing: AuthorityStore = {
      storeIdentity: () => store.storeIdentity(), loadAuthority: () => store.loadAuthority(),
      readReceipt: id => store.readReceipt(id), scanCommitted: (cursor, limit) => store.scanCommitted(cursor, limit),
      commitAuthority: async commit => {
        raced = true;
        assert.equal((await capture(contender, {...request, operation_id: "winner"})).status, "applied");
        return store.commitAuthority(commit);
      },
    };
    const lost = await capture(racing, request);
    assert.equal(raced, true);
    assert.equal(lost.status, "conflict");
    assert.equal((await store.readReceipt(request.operation_id)).status, "missing");
    const after = await head(store);
    assert.equal((after.head.todos as JsonObject[]).length, 2);
    const retry = await capture(store, request);
    assert.equal(retry.status, "no_change");
    assert.equal(retry.recorded_count, 0);
    assert.deepEqual((await head(store)).head, after.head);
  });
}
