/** One batch selection rule for legacy Markdown and canonical authority.
 * Capture declares unclaimed work; it never grants execution or closes a Todo. */
import type {JsonObject} from "../effect_program.ts";
import {requireJsonObject, requireStringArray, requireNonEmptyString, requireBoolean} from "../runtime_decode.ts";
import {EffectRuntimeRequestError} from "../effect_runtime_errors.ts";
import {compactPythonWhitespace} from "../coordination/todo_agents.ts";
import {canonicalAuthoritySha256, requireAuthorityStoreId} from "../coordination/authority_store_codec.ts";
import {AGENT_TODO_TASK_CLASSES} from "./authoring_scope.ts";
import {normalizeTodoWorkRequirements} from "./work_requirements.ts";
import {normalizeTodoRequiredDecisionScopes} from "./decision_metadata.ts";
import {TODO_CONTINUATION_POLICIES} from "./completion_policy.ts";

export const FOLLOWUP_CAPTURE_PLAN_SCHEMA = "todo_followup_capture_plan_request_v0";
export const FOLLOWUP_CAPTURE_RESULT_SCHEMA = "todo_followup_capture_result_v0";
export const MAX_CAPTURED_FOLLOWUPS = 2;

type UnsafeReason = "local_absolute_path" | "local_state_path" | "credential_literal" | "internal_only_marker";
type SkipReason = "empty" | "duplicate" | "max_items_exceeded" | `unsafe_boundary:${UnsafeReason}`;
// Compatibility heuristics for this public-safe capture command, not a DLP
// classifier or authorization rule. Keep reasons typed and limitations explicit.
const UNSAFE_PATTERNS: readonly [UnsafeReason, RegExp][] = [
  ["local_absolute_path", /(?:\/Users\/|\/private\/|\/var\/folders\/|file:\/\/)/iu],
  ["local_state_path", /(?:^|[\s"'`])(?:\.local\/|\.codex\/|\.loopx\/)/iu],
  ["credential_literal", /(?<![\p{L}\p{N}_])(?:api[_-]?key|secret|password|token)\s*[:=]/iu],
  ["internal_only_marker", /(?<![\p{L}\p{N}_])internal[-_\s]?only(?![\p{L}\p{N}_])/iu],
];

function unsafeReason(text: string): UnsafeReason | null {
  return UNSAFE_PATTERNS.find(([, pattern]) => pattern.test(text))?.[0] ?? null;
}

export interface FollowupCaptureIntent extends JsonObject {
  followups: string[];
  evidence: string;
  metadata: JsonObject;
}

/** Validate the entire request before selection, including a malformed tail or
 * metadata on an all-duplicate batch. Invalid members must not be dropped. */
export function normalizeFollowupCaptureIntent(value: unknown): FollowupCaptureIntent {
  const raw = requireJsonObject(value, "follow-up capture intent");
  for (const key of Object.keys(raw)) {
    if (!["followups", "evidence", "metadata"].includes(key)) throw new EffectRuntimeRequestError(`unsupported capture field: ${key}`);
  }
  const followups = requireStringArray(raw.followups, "followups").map(compactPythonWhitespace);
  if (!followups.length) throw new EffectRuntimeRequestError("todo capture-followups requires at least one --follow-up");
  const evidence = compactPythonWhitespace(typeof raw.evidence === "string" ? raw.evidence : "");
  if (!evidence) throw new EffectRuntimeRequestError("todo capture-followups requires --evidence with a public-safe pointer");
  const unsafe = unsafeReason(evidence);
  if (unsafe) throw new EffectRuntimeRequestError(`todo capture-followups evidence is not public-safe: ${unsafe}`);
  const input = requireJsonObject(raw.metadata, "follow-up metadata");
  for (const key of Object.keys(input)) {
    if (!["task_class", "action_kind", "continuation_policy", "required_write_scopes", "required_capabilities",
      "target_capabilities", "required_decision_scopes"].includes(key)) throw new EffectRuntimeRequestError(`unsupported capture metadata: ${key}`);
  }
  const taskClass = input.task_class == null ? "advancement_task"
    : compactPythonWhitespace(requireNonEmptyString(input.task_class, "task_class")).toLowerCase();
  if (!AGENT_TODO_TASK_CLASSES.has(taskClass)) throw new EffectRuntimeRequestError("capture-followups requires an agent task_class");
  const metadata: JsonObject = {task_class: taskClass, ...normalizeTodoWorkRequirements(input)};
  if (input.continuation_policy != null) {
    const policy = compactPythonWhitespace(requireNonEmptyString(input.continuation_policy, "continuation_policy")).toLowerCase();
    if (!(TODO_CONTINUATION_POLICIES as readonly string[]).includes(policy)) throw new EffectRuntimeRequestError("unsupported continuation_policy");
    metadata.continuation_policy = policy;
  }
  const scopes = normalizeTodoRequiredDecisionScopes(input.required_decision_scopes);
  if (scopes !== null) metadata.required_decision_scopes = scopes;
  return {followups, evidence, metadata};
}

export function planFollowupCapture(value: unknown): JsonObject {
  const request = requireJsonObject(value, "follow-up capture plan request");
  if (request.schema_version !== FOLLOWUP_CAPTURE_PLAN_SCHEMA) throw new EffectRuntimeRequestError("follow-up capture plan schema mismatch");
  for (const key of Object.keys(request)) {
    if (!["schema_version", "goal_id", "operation_id", "updated_at", "dry_run", "intent", "existing_texts"].includes(key)) throw new EffectRuntimeRequestError(`unsupported capture plan field: ${key}`);
  }
  const goal = requireAuthorityStoreId(request.goal_id, "goal id");
  const operation = requireAuthorityStoreId(request.operation_id, "operation id");
  const updatedAt = requireNonEmptyString(request.updated_at, "updated_at");
  if (Number.isNaN(Date.parse(updatedAt))) throw new EffectRuntimeRequestError("updated_at must be a timestamp");
  const dryRun = requireBoolean(request.dry_run, "dry_run");
  const intent = normalizeFollowupCaptureIntent(request.intent);
  const existing = new Set(requireStringArray(request.existing_texts, "existing_texts").map(compactPythonWhitespace));
  const items: JsonObject[] = [];
  let recorded = 0;
  for (const [index, text] of intent.followups.entries()) {
    let reason: SkipReason | null = null;
    const unsafe = unsafeReason(text);
    if (!text) reason = "empty";
    else if (unsafe) reason = `unsafe_boundary:${unsafe}`;
    else if (existing.has(text)) reason = "duplicate";
    else if (recorded >= MAX_CAPTURED_FOLLOWUPS) reason = "max_items_exceeded";
    const item: JsonObject = {todo: text, added: reason === null, already_exists: reason === "duplicate",
      skipped: reason !== null, skipped_reason: reason};
    if (reason === null) {
      recorded += 1;
      existing.add(text);
      Object.assign(item, intent.metadata, {todo_id: `todo_${canonicalAuthoritySha256({goal, operation, index}).slice(0, 24)}`,
        role: "agent", section: "Agent Todo", status: "open", changed: true,
        metadata_updated: false, status_changed: false, evidence: intent.evidence, updated_at: updatedAt});
    }
    items.push(item);
  }
  return {schema_version: FOLLOWUP_CAPTURE_RESULT_SCHEMA, ok: true, dry_run: dryRun, changed: recorded > 0,
    goal_id: goal, role: "agent", section: "Agent Todo", max_items: MAX_CAPTURED_FOLLOWUPS,
    requested_count: items.length, recorded_count: recorded, skipped_count: items.length - recorded,
    evidence: intent.evidence, items, updated_at: recorded ? updatedAt : null};
}
