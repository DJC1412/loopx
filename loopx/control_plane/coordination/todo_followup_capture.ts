/** Batch selection, record admission and one CAS/receipt over the complete head. */
import type {JsonObject} from "../effect_program.ts";
import type {AuthorityStore} from "./authority_store.ts";
import {canonicalAuthorityObject, canonicalAuthoritySha256, requireAuthorityStoreId} from "./authority_store_codec.ts";
import {CoordinationCommandReceipt, commandReceiptResult} from "./command_receipt.ts";
import {AUTHORITY_SOURCE_CHANGED, uncheckedAuthoritySource, type AuthoritySourceCheck} from "./authority_source.ts";
import {indexCoordinationProjection, prepareCoordinationProjectionCommit, validateCoordinationTodoReadModel} from "./coordination_projection.ts";
import {planCoordinationTodoCreate} from "./todo_create.ts";
import {TODO_DOMAIN_ITEM_SCHEMA} from "./coordination_state_contract.ts";
import {FOLLOWUP_CAPTURE_PLAN_SCHEMA, normalizeFollowupCaptureIntent, planFollowupCapture} from "../todos/followup_capture.ts";
import {requireBoolean} from "../runtime_decode.ts";

export const COORDINATION_FOLLOWUP_CAPTURE_SCHEMA = "loopx_coordination_followup_capture_request_v0";
export const COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA = "loopx_coordination_followup_capture_result_v0";
const RECEIPT_SCHEMA = "loopx_coordination_followup_capture_receipt_v0";

export interface CoordinationFollowupCaptureInput {
  goal_id: string;
  operation_id: string;
  intent: JsonObject;
  dry_run: boolean;
  now: Date;
}

function failure(reason_code: string, reason: string): JsonObject & {schema_version: typeof COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA} {
  return {schema_version: COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA, status: "failed", changed: false, reason_code, reason};
}

export async function executeCoordinationFollowupCapture(store: AuthorityStore,
  raw: CoordinationFollowupCaptureInput,
  authoritySourcesCurrent: AuthoritySourceCheck = uncheckedAuthoritySource): Promise<JsonObject> {
  let input: CoordinationFollowupCaptureInput;
  try {
    input = {goal_id: requireAuthorityStoreId(raw.goal_id, "goal id"),
      operation_id: requireAuthorityStoreId(raw.operation_id, "operation id"),
      intent: normalizeFollowupCaptureIntent(raw.intent), dry_run: requireBoolean(raw.dry_run, "dry_run"), now: raw.now};
    if (!(input.now instanceof Date) || Number.isNaN(input.now.valueOf())) throw new Error("now must be a valid Date");
  } catch (error) { return failure("invalid_followup_capture_request", error instanceof Error ? error.message : String(error)); }
  // Clock and source witness are observations, not a caller's retry identity.
  const identity = {schema_version: RECEIPT_SCHEMA, goal_id: input.goal_id, operation_id: input.operation_id,
    request_sha256: canonicalAuthoritySha256({goal_id: input.goal_id, intent: input.intent, dry_run: input.dry_run})};
  const receipt = new CoordinationCommandReceipt({result_schema: COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA,
    identity, failure, decode: commandReceiptResult});
  const previous = await receipt.read(store);
  if (previous) return previous;
  if (!await authoritySourcesCurrent()) return failure(AUTHORITY_SOURCE_CHANGED.code, AUTHORITY_SOURCE_CHANGED.reason);
  const head = await store.loadAuthority();
  if (head.status !== "loaded") return {schema_version: COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA, ...head, changed: false};
  let result: JsonObject;
  const mutations: {kind: "todo_upsert"; todo: JsonObject}[] = [];
  try {
    const indexed = indexCoordinationProjection(head.head, input.goal_id);
    validateCoordinationTodoReadModel(head.head, input.goal_id);
    const model = canonicalAuthorityObject(head.head.todo_read_model, "Todo read model");
    result = planFollowupCapture({schema_version: FOLLOWUP_CAPTURE_PLAN_SCHEMA, goal_id: input.goal_id,
      operation_id: input.operation_id, intent: input.intent, dry_run: input.dry_run,
      updated_at: input.now.toISOString().replace(/\.\d{3}Z$/u, "Z"),
      // Done and deferred active rows still suppress repeated capture. Archive
      // and User rows do not. The full head, never its display slice, owns this.
      existing_texts: [...indexed.todos.values()].filter(todo => todo.role === "agent" && todo.archive_state === "active").map(todo => todo.text)});
    for (const rawItem of result.items as JsonObject[]) {
      if (!rawItem.added) continue;
      const item = canonicalAuthorityObject(rawItem, "captured Todo");
      const metadata = canonicalAuthorityObject(input.intent.metadata, "capture metadata");
      const created = planCoordinationTodoCreate({...input, actor_agent_id: null, registered_agents: [],
        todo: {schema_version: TODO_DOMAIN_ITEM_SCHEMA, ...metadata, todo_id: item.todo_id,
          text: item.todo, role: "agent", status: "open", done: false, archive_state: "active",
          evidence: input.intent.evidence}}, indexed.todos, model.schema_version, "operation_lane");
      if (created.status !== "planned") throw new Error(String(created.reason));
      mutations.push({kind: "todo_upsert", todo: canonicalAuthorityObject(created.todo, "captured record")});
    }
  } catch (error) { return failure("followup_capture_rejected", error instanceof Error ? error.message : String(error)); }
  if (!await authoritySourcesCurrent()) return failure(AUTHORITY_SOURCE_CHANGED.code, AUTHORITY_SOURCE_CHANGED.reason);
  if (input.dry_run) return {...result, schema_version: COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA,
    status: "planned", provider_revision: head.provider_revision};
  // Even a no-op seals its original decision. Replaying it after later edits
  // must never become permission to create work that was previously skipped.
  const commit = mutations.length ? prepareCoordinationProjectionCommit({goal_id: input.goal_id, operation_id: input.operation_id,
    expected_provider_revision: head.provider_revision, projection: head.head, mutations}) : {operation_id: input.operation_id,
    expected_provider_revision: head.provider_revision, next_projection: head.head, events: [], receipts: []};
  commit.receipts = [{...identity, result}];
  return receipt.commit(store, commit);
}
