/** Team-plan admission and assignment transaction. A committed assignment is
 * neither receiver adoption nor a lease, quota grant, or execution receipt. */
import {EffectRuntimeRequestError} from "../effect_runtime_errors.ts";
import type {JsonObject} from "../effect_program.ts";
import {requireJsonObject, requireStringArray} from "../runtime_decode.ts";
import {canonicalAuthoritySha256, requireAuthorityStoreId} from "../coordination/authority_store_codec.ts";
import {planCoordinationTodoCreate} from "../coordination/todo_create.ts";
import {TODO_DOMAIN_ITEM_SCHEMA, TODO_DOMAIN_READ_RECORD_SCHEMA} from "../coordination/coordination_state_contract.ts";

export const TEAM_PLAN_SCHEMA = "steward_team_plan_preview_v0";
export const TEAM_PLAN_KIND = "steward_team_plan_preview";
export const TEAM_TRANSACTION_SCHEMA = "steward_team_plan_transaction_v0";
const priorities = new Set(["P0", "P1", "P2", "P3"]);
const declaredGaps = new Set(["agent_not_registered", "capability_not_granted", "audience_not_authorized"]);
const hostGaps = new Set([...declaredGaps, "action_kind_not_supported"]);
function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim() || value.trim().length > 600) throw new EffectRuntimeRequestError(`${label} must be a non-empty string`);
  return value.trim().replace(/\s+/gu, " ");
}
function todo(value: unknown): JsonObject {
  const item = requireJsonObject(value, "first_todo");
  if (!priorities.has(String(item.priority))) throw new EffectRuntimeRequestError("first_todo priority is invalid");
  if (item.task_class !== "advancement_task") throw new EffectRuntimeRequestError("a lane's first bounded Todo must be an advancement_task");
  return {text: text(item.text, "first_todo text"), priority: item.priority,
    task_class: item.task_class, action_kind: text(item.action_kind, "first_todo action_kind")};
}

export function previewTeamPlan(value: unknown): JsonObject {
  const request = requireJsonObject(value, "team plan preview request");
  const plan = requireJsonObject(request.plan, "team plan");
  const registered = new Set(requireStringArray(request.registered_agents, "registered_agents"));
  const actions = new Set(requireStringArray(request.supported_action_kinds, "supported_action_kinds"));
  if (plan.schema_version !== TEAM_PLAN_SCHEMA) throw new EffectRuntimeRequestError("steward team plan preview schema_version is invalid");
  if (plan.kind !== TEAM_PLAN_KIND) throw new EffectRuntimeRequestError("steward team plan preview kind is invalid");
  const goal = text(plan.goal_id, "goal_id");
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$/u.test(goal)) throw new EffectRuntimeRequestError("steward team plan preview requires an exact Goal id");
  if (!Array.isArray(plan.lanes) || plan.lanes.length < 1 || plan.lanes.length > 8) throw new EffectRuntimeRequestError("steward team plan preview requires 1..8 lanes");
  const seen = new Set<string>();
  const gaps: JsonObject[] = [];
  const lanes = plan.lanes.map(raw => {
    const lane = requireJsonObject(raw, "lane");
    const id = text(lane.lane_id, "lane_id");
    if (seen.has(id)) throw new EffectRuntimeRequestError("steward team plan preview repeats a lane_id");
    seen.add(id);
    const agent = text(lane.agent_id, "agent_id");
    const base = {lane_id: id, agent_id: agent, acceptance: text(lane.acceptance, "lane acceptance")};
    let reason: string | null = null;
    let declined: JsonObject | null = null;
    let note: string | null = null;
    if (lane.staffing_gap != null) {
      const gap = requireJsonObject(lane.staffing_gap, "staffing_gap");
      reason = String(gap.reason_code);
      if (!declaredGaps.has(reason)) throw new EffectRuntimeRequestError("staffing_gap reason_code is invalid");
      if (lane.first_todo != null) throw new EffectRuntimeRequestError("a lane that declares a gap may not declare work");
      note = text(gap.note, "staffing_gap note");
    } else if (lane.first_todo == null) {
      reason = String(lane.gap_reason_code);
      if (!hostGaps.has(reason)) throw new EffectRuntimeRequestError("lane gap_reason_code is invalid");
      if (lane.declined_first_todo != null) declined = todo(lane.declined_first_todo);
      if (lane.gap_note != null) note = text(lane.gap_note, "gap_note");
      if (!declined && !note) throw new EffectRuntimeRequestError("a lane without work must keep why it is a gap");
    } else {
      const first = todo(lane.first_todo);
      reason = !registered.has(agent) ? "agent_not_registered" : !actions.has(String(first.action_kind)) ? "action_kind_not_supported" : null;
      if (!reason) return {...base, staffing: "ready", first_todo: first};
      declined = first;
    }
    gaps.push({lane_id: id, reason_code: reason});
    return {...base, staffing: "gap", gap_reason_code: reason,
      ...(declined ? {declined_first_todo: declined} : {}), ...(note ? {gap_note: note} : {})};
  });
  const envelope = requireJsonObject(plan.quota_envelope, "quota_envelope");
  if (!Object.keys(envelope).length) throw new EffectRuntimeRequestError("steward team plan preview requires a quota envelope");
  // These values are planning context. There is no quota/stop-policy writer in
  // this operation, so explicit enforcement intent must never be ignored.
  if (plan.enforcement != null) {
    for (const [field, claim] of Object.entries(requireJsonObject(plan.enforcement, "enforcement"))) {
      if (!["quota_envelope", "stop_condition"].includes(field) || claim !== "advisory") {
        throw new EffectRuntimeRequestError(`team plan cannot enforce ${field}; only advisory planning values are supported`);
      }
    }
  }
  return {schema_version: TEAM_PLAN_SCHEMA, kind: TEAM_PLAN_KIND, goal_id: goal,
    objective: text(plan.objective, "objective"), lanes, gaps, quota_envelope: envelope,
    stop_condition: text(plan.stop_condition, "stop_condition"), applies: false};
}

export function teamTransactionIdentity(request: JsonObject) {
  const plan = requireJsonObject(request.plan, "plan");
  const goal = requireAuthorityStoreId(request.goal_id, "goal id");
  if (plan.goal_id !== goal) throw new EffectRuntimeRequestError("steward team plan proposal names a different Goal than its settlement");
  const operation = `team-plan:${canonicalAuthoritySha256({goal, proposal: requireAuthorityStoreId(plan.proposal_id, "proposal id")})}`;
  return {schema_version: TEAM_TRANSACTION_SCHEMA, goal_id: goal, operation_id: operation,
    request_sha256: canonicalAuthoritySha256({plan, actor_agent_id: request.actor_agent_id ?? null,
      expected_state_fingerprint: request.expected_state_fingerprint ?? null})};
}

function laneTodoId(operation: string, lane: unknown): string {
  return `todo_${canonicalAuthoritySha256({operation, lane}).slice(0, 24)}`;
}

/** Replay is historical readback; current Todo edits/completion/deletion are
 * never an invitation to recreate or overwrite work from the original plan. */
export function replayTeamTransaction(request: JsonObject, previous: JsonObject): JsonObject {
  const identity = teamTransactionIdentity(request);
  for (const [key, value] of Object.entries(identity)) {
    if (previous[key] !== value) throw new EffectRuntimeRequestError("team plan operation identity mismatch");
  }
  const result = requireJsonObject(previous.result, "team transaction result");
  const lanes = requireJsonObject(request.plan, "plan").lanes;
  if (!Array.isArray(lanes) || !Array.isArray(result.lane_settlements) ||
      !Array.isArray(result.lane_todo_ids) || !Array.isArray(result.gap_lanes) ||
      result.lane_settlements.length === 0 ||
      result.lane_settlements.length + result.gap_lanes.length !== lanes.length ||
      result.gap_count !== result.gap_lanes.length) throw new EffectRuntimeRequestError("invalid team plan receipt");
  const ids = result.lane_settlements.map(raw => {
    const settled = requireJsonObject(raw, "lane settlement");
    const lane = lanes.map(rawLane => requireJsonObject(rawLane, "lane")).find(item => item.lane_id === settled.lane_id);
    if (!lane || settled.todo_id !== laneTodoId(identity.operation_id, lane.lane_id) ||
        settled.agent_id !== lane.agent_id || settled.acceptance !== lane.acceptance) {
      throw new EffectRuntimeRequestError("team plan receipt lane identity mismatch");
    }
    return settled.todo_id;
  });
  if (new Set(ids).size !== ids.length || canonicalAuthoritySha256(ids) !== canonicalAuthoritySha256(result.lane_todo_ids)) {
    throw new EffectRuntimeRequestError("team plan receipt Todo identities mismatch");
  }
  return {...result, action: "reused", created_todo_ids: [],
    reused_lane_count: (result.lane_todo_ids as unknown[]).length,
    lane_settlements: (result.lane_settlements as JsonObject[]).map(lane => ({...lane, disposition: "reused"}))};
}

/** Pure whole-batch planner shared by legacy Markdown and AuthorityStore. */
export function planTeamTransaction(value: unknown): JsonObject {
  const request = requireJsonObject(value, "team transaction");
  const identity = teamTransactionIdentity(request);
  if (request.previous_receipt != null) return {replayed: true,
    result: replayTeamTransaction(request, requireJsonObject(request.previous_receipt, "previous receipt")), todos: []};
  if (request.expected_state_fingerprint != null && request.expected_state_fingerprint !== request.current_state_fingerprint) throw new EffectRuntimeRequestError("team plan state changed after preview", "team_plan_preview_stale");
  const preview = previewTeamPlan(request);
  const records = request.todos == null ? [] : request.todos;
  if (!Array.isArray(records)) throw new EffectRuntimeRequestError("todos must be an array");
  const current = new Map(records.map(raw => {const row = requireJsonObject(raw, "Todo"); return [String(row.todo_id), row];}));
  const now = new Date(text(request.observed_at, "observed_at"));
  const registered = requireStringArray(request.registered_agents, "registered_agents");
  const actor = request.actor_agent_id == null ? null : text(request.actor_agent_id, "actor_agent_id");
  if (actor && !registered.includes(actor)) throw new EffectRuntimeRequestError("team plan author is not registered");
  const todos: JsonObject[] = [];
  const settlements: JsonObject[] = [];
  for (const lane of preview.lanes as JsonObject[]) {
    if (lane.staffing !== "ready") continue;
    const first = requireJsonObject(lane.first_todo, "first Todo");
    const id = laneTodoId(identity.operation_id, lane.lane_id);
    const rawText = String(first.text).replace(/^\s*[-*]\s+\[[ xX-]\]\s*/u, "");
    const record: JsonObject = {schema_version: TODO_DOMAIN_ITEM_SCHEMA, todo_id: id, role: "agent",
      status: "open", done: false, archive_state: "active", task_class: "advancement_task",
      action_kind: first.action_kind, text: /^\[P[0-4]\]\s+/iu.test(rawText) ? rawText : `[${first.priority}] ${rawText}`};
    // An initial assignment reserves a lane; it does not impersonate the
    // receiver as author and does not create an adoption or execution lease.
    // Only an owner-originated confirmation can assign another registered peer.
    if (actor && actor !== lane.agent_id) throw new EffectRuntimeRequestError("assigning another Agent requires owner confirmation");
    record.claimed_by = lane.agent_id;
    // Native records retain the same priority/title facts as the Markdown
    // reader; projection validates these rather than manufacturing authority.
    const label = /^\[(P[0-4])\]\s+(.+)$/iu.exec(String(record.text))!;
    record.priority = label[1]!.toUpperCase();
    record.title = label[2]!;
    const planned = planCoordinationTodoCreate({goal_id: String(identity.goal_id), operation_id: String(identity.operation_id),
      actor_agent_id: actor, registered_agents: registered, dry_run: false, now, todo: record},
    current, request.read_model_schema ?? TODO_DOMAIN_READ_RECORD_SCHEMA, "operation_lane");
    if (planned.status !== "planned") throw new EffectRuntimeRequestError(String(planned.reason));
    const created = requireJsonObject(planned.todo, "created Todo");
    todos.push(created); current.set(id, created);
    settlements.push({lane_id: lane.lane_id, agent_id: lane.agent_id, priority: first.priority,
      acceptance: lane.acceptance, disposition: "created", todo_id: id});
  }
  const ids = todos.map(row => row.todo_id);
  const gaps = preview.gaps as JsonObject[];
  const result: JsonObject = {action: ids.length ? "created" : "unstaffed", todo_id: ids[0] ?? "",
    target_key: null, created_todo_ids: ids, lane_todo_ids: ids, lane_settlements: settlements,
    gap_count: gaps.length, gap_lanes: gaps.map(gap => ({...gap,
      agent_id: (preview.lanes as JsonObject[]).find(lane => lane.lane_id === gap.lane_id)!.agent_id})), reused_lane_count: 0,
    intent_basis: request.intent_basis ?? null, lane_failure: null};
  return {replayed: false, todos, result, receipt: {...identity, result}};
}
