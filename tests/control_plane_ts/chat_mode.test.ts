import assert from "node:assert/strict";
import test from "node:test";
import {planChatMode} from "../../loopx/control_plane/collaboration/chat_mode.ts";
import type {JsonObject} from "../../loopx/control_plane/effect_program.ts";

const session = {agent_id: "codex", goal_id: "research", channel_id: "goal.research"};
const input: JsonObject = {
  session, origin: "web", operation: "start", native: {status: "absent"},
  settings: {agent_id: "lead", token_budget: 1000}, registered_agents: ["lead"],
  goal_active: true, execution_binding_valid: true,
};

test("execution is explicit and preserves native lifecycle/allowance", () => {
  assert.equal(planChatMode(input).enabled, true);
  assert.equal(planChatMode({...input, operation: "configure"}).enabled, false);
  assert.equal(planChatMode({...input, operation: "exit"}).enabled, false);
  assert.throws(() => planChatMode({...input, operation: "pause"}), /enabled/);
  assert.equal(planChatMode({...input, operation: "pause", session: {...session, loopx_mode: {enabled: true}}}).enabled, true);
  assert.throws(() => planChatMode({...input, native: {status: "paused"}}), /unfinished/);
  assert.equal(planChatMode({...input, operation: "resume", native: {status: "budgetLimited", tokensUsed: 999}}).enabled, true);
  assert.throws(() => planChatMode({...input, operation: "resume", native: {status: "paused", tokensUsed: 1000}}), /consumed/);
  assert.throws(() => planChatMode({...input, operation: "resume", native: {status: "active"}}), /paused/);
});

test("neither registration nor a role name grants execution", () => {
  for (const changes of [
    {origin: "external"}, {goal_active: false}, {execution_binding_valid: false},
    {registered_agents: []}, {settings: {agent_id: "lead", token_budget: 0}},
    {settings: {agent_id: "lead", token_budget: true}},
    {session: {...session, active_turn_id: "other"}},
    {session: {...session, session_mode: "attached_host"}},
    {session: {...session, channel_id: "manager"}},
  ]) assert.throws(() => planChatMode({...input, ...changes}));
});

test("all three delivery modes require the exact active execution turn", () => {
  const active = {...session, active_turn_id: "one", loopx_mode: {enabled: true}};
  const message: JsonObject = {...input, operation: "message", session: active,
    turn: {turn_id: "one", loopx_execution: true}};
  for (const delivery_mode of ["queue", "inbox", "steer"]) {
    assert.equal(planChatMode({...message, delivery_mode}).delivery_mode, delivery_mode);
  }
  assert.throws(() => planChatMode({...message, delivery_mode: "unknown"}));
  assert.throws(() => planChatMode({...message, delivery_mode: "queue", turn: {turn_id: "one"}}));
  assert.throws(() => planChatMode({...message, delivery_mode: "queue", turn: {turn_id: "other", loopx_execution: true}}));
  assert.throws(() => planChatMode({...message, delivery_mode: "queue", session: {...active, loopx_mode: {enabled: true, paused: true}}}));
});
