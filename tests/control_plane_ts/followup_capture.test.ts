import assert from "node:assert/strict";
import test from "node:test";
import type {JsonObject} from "../../loopx/control_plane/effect_program.ts";
import {planFollowupCapture, FOLLOWUP_CAPTURE_PLAN_SCHEMA} from "../../loopx/control_plane/todos/followup_capture.ts";
const input = {schema_version: FOLLOWUP_CAPTURE_PLAN_SCHEMA, goal_id: "goal-a", operation_id: "batch",
  updated_at: "2026-09-07T07:00:00Z", dry_run: true, existing_texts: [],
  intent: {followups: ["One task"], evidence: "validation://public", metadata: {}}};

test("capture retains Python whitespace and Unicode word boundaries without lossy identity", () => {
  const result = planFollowupCapture({...input, existing_texts: ["Already captured"], intent: {...input.intent,
    followups: ["\u001c", "Already\u0085captured", "αtoken" + "=prose", "internal-onlyβ", "token" + "=fixture", "internal only"]}});
  assert.deepEqual((result.items as JsonObject[]).map(i => i.skipped_reason),
    ["empty", "duplicate", null, null, "unsafe_boundary:credential_literal", "unsafe_boundary:internal_only_marker"]);
});

test("capture validates malformed tails and unknown authority fields before any plan", () => {
  for (const invalid of [
    {...input, actor_agent_id: "agent-a"},
    {...input, intent: {...input.intent, followups: ["One", "Two", null]}},
    {...input, intent: {...input.intent, metadata: {claimed_by: "agent-a"}}},
    {...input, intent: {...input.intent, metadata: {task_class: "user_gate"}}},
    {...input, intent: {...input.intent, metadata: {required_decision_scopes: ["direction:goal:valid", "bad"]}}},
  ]) assert.throws(() => planFollowupCapture(invalid));
});
