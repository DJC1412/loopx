/** Conversation execution admission. Native lifecycle is a host observation;
 * this contract never settles canonical work or grants another Agent's lease. */
import type {JsonObject} from "../effect_program.ts";
import {EffectRuntimeRequestError} from "../effect_runtime_errors.ts";
import {requireJsonObject} from "../runtime_decode.ts";
import {resolveConversationScope} from "./conversation_scope.ts";

function requireThat(ok: unknown, message: string): asserts ok {
  if (!ok) throw new EffectRuntimeRequestError(message);
}

export function planChatMode(input: JsonObject): JsonObject {
  const session = requireJsonObject(input.session, "conversation session");
  const operation = input.operation;
  requireThat(["configure", "start", "resume", "pause", "exit", "message"].includes(String(operation)), "unsupported conversation operation");
  requireThat(resolveConversationScope(session).kind === "owner_goal"
    && input.origin === "web" && session.session_mode !== "attached_host"
    && session.agent_id === "codex", "LoopX mode requires a local managed Codex Goal conversation");
  const settings = requireJsonObject(input.settings, "conversation settings");
  const native = requireJsonObject(input.native ?? {}, "native Goal observation");
  if (operation === "message") {
    const mode = requireJsonObject(session.loopx_mode ?? {}, "mode");
    const turn = requireJsonObject(input.turn ?? {}, "active execution turn");
    requireThat(mode.enabled === true && mode.paused !== true && !!session.active_turn_id
      && turn.loopx_execution === true && turn.turn_id === session.active_turn_id,
      "LoopX message delivery requires active conversation execution");
    requireThat(["queue", "inbox", "steer"].includes(String(input.delivery_mode)), "unsupported delivery mode");
    return {operation, delivery_mode: input.delivery_mode};
  }
  if (operation === "pause" || operation === "exit") {
    if (operation === "pause") requireThat(requireJsonObject(session.loopx_mode ?? {}, "mode").enabled === true,
      "pause requires enabled conversation execution");
    return {operation, enabled: operation !== "exit"};
  }
  requireThat(input.goal_active === true, "Goal is stopped or unavailable");
  requireThat(!session.active_turn_id, "wait for the current conversation turn before changing execution");
  requireThat(Number.isSafeInteger(settings.token_budget) && Number(settings.token_budget) > 0
    && Number(settings.token_budget) <= 2147483647, "set a positive coordinator token allowance");
  requireThat(typeof settings.agent_id === "string" && Array.isArray(input.registered_agents)
    && input.registered_agents.includes(settings.agent_id), "select a registered coordinator identity");
  requireThat(input.execution_binding_valid === true, "configure the coordinator's authorized execution bindings first");
  if (operation === "start") requireThat(!native.status || ["absent", "complete"].includes(String(native.status)),
    "resume the unfinished native Goal instead of replacing it");
  if (operation === "resume") {
    requireThat(["paused", "blocked", "usageLimited", "budgetLimited"].includes(String(native.status)),
      "resume requires a paused, blocked or limited native Goal");
    requireThat(Number(settings.token_budget) > Number(native.tokensUsed ?? 0), "total allowance must exceed consumed tokens");
  }
  return {operation, enabled: operation !== "configure", settings};
}
