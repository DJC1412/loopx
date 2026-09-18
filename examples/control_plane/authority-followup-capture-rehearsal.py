#!/usr/bin/env python3
"""Rehearse batch capture on a read-only Goal snapshot and disposable providers.

The source is never promoted or rewritten. Output contains counts and digests;
raw source, identifiers, paths and diagnostics stay outside the public report.
PostgreSQL must be an explicitly isolated server, not an active service tenant.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from loopx.control_plane.coordination.runtime_shadow import build_runtime_shadow_source_snapshot  # noqa: E402
from loopx.history import load_registry  # noqa: E402
from loopx.paths import resolve_runtime_root  # noqa: E402
from loopx.state_refresh import resolve_goal_state  # noqa: E402
from loopx.todo_followups import capture_followup_todos  # noqa: E402

NODE_REHEARSAL = r"""
import assert from 'node:assert/strict';
import {createHash,randomUUID} from 'node:crypto';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {Pool} from 'pg';
let raw=''; for await(const chunk of process.stdin) raw+=chunk;
const input=JSON.parse(raw), goal=input.goal_id;
const at=path=>import(pathToFileURL(join(input.repo,'loopx/control_plane',path)).href);
const {captureLocalFollowups:capture}=await at('coordination/followup_capture_runtime.ts');
const {openLocalAuthorityStore,selectLocalSqliteAuthority}=await at('coordination/local_authority_provider.ts');
const {PostgreSqlAuthorityStore,installPostgreSqlAuthorityStoreSchema}=await at('coordination/postgresql_authority_store.ts');
const {PostgreSqlAuthorityService}=await at('coordination/postgresql_authority_service.ts');
const {engageLegacyCoordinationWriterFence}=await at('coordination/legacy_writer_fence.ts');
const {canonicalAuthoritySha256:digest}=await at('coordination/authority_store_codec.ts');
const root=await mkdtemp(join(tmpdir(),'loopx-capture-rehearsal-'));
const pool=new Pool({connectionString:process.env.LOOPX_TEST_POSTGRES_URL,max:4});
const database={connect:async()=>{const c=await pool.connect();return {query:(t,v)=>c.query(t,v),release:e=>c.release(e)};}};
const tenant=`capture-rehearsal-${randomUUID()}`, reports={}, semantics={};
try {
  await installPostgreSqlAuthorityStoreSchema(database,`postgresql:${'b'.repeat(32)}`);
  for(const arm of ['file','sqlite','postgresql']) {
    const runtime=join(root,arm);await mkdir(runtime,{recursive:true});
    const registry=join(runtime,'registry.json'), display=join(runtime,'state.md');
    await writeFile(registry,'{}'); await writeFile(display,'# Disposable display\n');
    let dependencies={};
    if(arm==='sqlite') assert.equal((await selectLocalSqliteAuthority(runtime,goal,true)).ok,true);
    if(arm==='postgresql') {
      const store=new PostgreSqlAuthorityStore(database,{tenant_id:tenant,goal_id:goal});
      const identity=await store.storeIdentity();assert.equal(identity.status,'available');
      await mkdir(join(runtime,'authority'),{recursive:true});
      await writeFile(join(runtime,'authority',`provider-${createHash('sha256').update(goal).digest('hex')}.json`),JSON.stringify({
        schema_version:'loopx_local_authority_provider_v0',provider:'postgresql',goal_id:goal,tenant_id:tenant,store_identity:identity.store_identity}));
      const service=new PostgreSqlAuthorityService({database,
        authenticatePrincipal:()=>({status:'authenticated',principal:{principal_id:'isolated-capture'}}),
        authorizeTenant:(principal,selected)=>principal==='isolated-capture'&&selected===tenant?{status:'allowed'}:
          {status:'denied',reason_code:'wrong_tenant',reason:'outside disposable tenant'}});
      dependencies={openPostgresqlStore:async selected=>{
        const opened=await service.openStore({credential:null,...selected});assert.equal(opened.status,'opened');return opened.store;}};
    }
    const store=await openLocalAuthorityStore(runtime,goal,dependencies);
    const seed=await store.commitAuthority({operation_id:'seed',expected_provider_revision:null,next_projection:input.projection,events:[],receipts:[]});
    assert.equal(seed.status,'applied');
    assert.equal((await engageLegacyCoordinationWriterFence({schema_version:'loopx_legacy_coordination_writer_fence_engage_request_v0',
      runtime_root:runtime,goal_id:goal,state_path:display,fence:{schema_version:'loopx_legacy_coordination_writer_fence_v0',state:'engaged',
        goal_id:goal,fence_id:'rehearsal',source_version:'snapshot',source_projection_sha256:digest(input.projection),
        expected_shadow_provider_revision:seed.provider_revision}})).status,'applied');
    await rm(display);
    const request={schema_version:'loopx_coordination_followup_capture_request_v0',runtime_root:runtime,goal_id:goal,
      operation_id:'rehearsal-batch',intent:input.intent,dry_run:false,
      registry_source:{path:registry,sha256:createHash('sha256').update('{}').digest('hex')}};
    const before=await store.loadAuthority();
    assert.equal((await capture({...request,dry_run:true},dependencies)).status,'planned');
    assert.deepEqual(await store.loadAuthority(),before);
    const applied=await capture(request,dependencies);assert.equal(applied.status,'applied');
    assert.equal(applied.recorded_count,2);assert.equal(applied.skipped_count,2);
    const after=await store.loadAuthority();assert.equal(after.status,'loaded');
    const originalIds=new Set(input.projection.todos.map(t=>t.todo_id));
    assert.deepEqual(after.head.todos.filter(t=>originalIds.has(t.todo_id)),input.projection.todos);
    assert.deepEqual(after.head.leases,input.projection.leases);
    const added=after.head.todos.filter(t=>!originalIds.has(t.todo_id));
    assert.equal(added.length,2);assert(added.every(t=>!t.claimed_by));
    const replay=await capture(request,dependencies);assert.equal(replay.status,'replayed');
    assert.deepEqual(replay.original_receipt,applied.original_receipt);
    assert.deepEqual(await store.loadAuthority(),after);
    const noop=await capture({...request,operation_id:'duplicate-batch',intent:{...input.intent,followups:input.intent.followups.slice(0,2)}},dependencies);
    assert.equal(noop.status,'no_change');assert.equal(noop.recorded_count,0);
    semantics[arm]=added.map(t=>({text:t.text,continuation_policy:t.continuation_policy,required_capabilities:t.required_capabilities})).sort((a,b)=>a.text.localeCompare(b.text));
    reports[arm]={added:added.length,skipped:applied.skipped_count,preview_no_write:true,replayed:true,no_op_sealed:true,existing_records_unchanged:true};
  }
  assert.deepEqual(semantics.file,semantics.sqlite);assert.deepEqual(semantics.file,semantics.postgresql);
  assert.deepEqual(semantics.file,input.legacy_semantics);
  process.stdout.write(JSON.stringify({schema_version:'authority_followup_capture_rehearsal_v0',
    source_todos:input.projection.todos.length,source_leases:input.projection.leases.length,
    semantic_sha256:digest(semantics.file),provider_semantics_equal:true,arms:reports}));
} finally {
  for(const table of ['authority_receipts','authority_events','authority_commits','authority_heads'])
    await pool.query(`DELETE FROM loopx_control_plane.${table} WHERE tenant_id=$1`,[tenant]);
  await pool.end();await rm(root,{recursive:true,force:true});
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--execute-isolated-postgresql", action="store_true")
    parser.add_argument("--private-diagnostics", type=Path)
    args = parser.parse_args()
    if not args.execute_isolated_postgresql or not os.environ.get("LOOPX_TEST_POSTGRES_URL"):
        raise SystemExit("an explicitly isolated PostgreSQL server is required")
    registry_path = args.registry.resolve()
    registry_bytes = registry_path.read_bytes()
    registry = load_registry(registry_path)
    goal = next(g for g in registry["goals"] if g["id"] == args.goal_id)
    runtime = resolve_runtime_root(registry, None, registry_path=registry_path)
    _, _, state = resolve_goal_state(registry=registry, goal_id=args.goal_id, project_override=None, state_file_override=None)
    projection, snapshot = build_runtime_shadow_source_snapshot(goal=goal, runtime_root=runtime, state_path=state, registry_path=registry_path)
    source_text = state.read_text()
    # Public synthetic deltas are intentionally independent of source wording.
    intent = {"followups": ["Validate isolated capture atomicity", "Validate isolated capture recovery",
        "Validate isolated capture atomicity", "Inspect file://rehearsal"],
        "evidence": "validation://isolated-capture", "metadata": {
            "continuation_policy": "same_agent_non_delivery", "required_capabilities": ["code_review"]}}
    with tempfile.TemporaryDirectory(prefix="loopx-legacy-capture-") as tmp:
        root = Path(tmp)
        cloned_state = root / "state.md"
        cloned_state.write_text(source_text)
        cloned_registry = root / "registry.json"
        cloned_registry.write_text(json.dumps({"common_runtime_root": str(root / "runtime"), "goals": [
            {"id": args.goal_id, "repo": str(root), "state_file": cloned_state.name}]}))
        legacy = capture_followup_todos(registry_path=cloned_registry, goal_id=args.goal_id,
            followups=intent["followups"], evidence=intent["evidence"], **intent["metadata"])
        assert legacy["recorded_count"] == 2 and legacy["skipped_count"] == 2
        semantics = sorted(({"text": item["todo"], "continuation_policy": item["continuation_policy"],
            "required_capabilities": item["required_capabilities"]} for item in legacy["items"] if item["added"]), key=lambda t: t["text"])
    child = subprocess.run(["node", "--no-warnings", "--experimental-sqlite", "--experimental-strip-types", "--input-type=module", "-e", NODE_REHEARSAL],
        input=json.dumps({"repo": str(REPOSITORY), "goal_id": args.goal_id, "projection": projection, "intent": intent, "legacy_semantics": semantics}),
        cwd=REPOSITORY, capture_output=True, text=True, timeout=180, check=False)
    if child.returncode:
        if args.private_diagnostics:
            descriptor = os.open(args.private_diagnostics, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(child.stderr)
        raise SystemExit("isolated capture rehearsal failed; diagnostics remain private")
    after_projection, after_snapshot = build_runtime_shadow_source_snapshot(goal=goal, runtime_root=runtime, state_path=state, registry_path=registry_path)
    if projection != after_projection or snapshot != after_snapshot or registry_path.read_bytes() != registry_bytes or state.read_text() != source_text:
        raise SystemExit("live source changed during rehearsal; retry from a stable snapshot")
    result = json.loads(child.stdout)
    result.update(source_unchanged=True, source_snapshot_sha256=hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
                  legacy={"added": 2, "skipped": 2, "semantics_equal": True})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
