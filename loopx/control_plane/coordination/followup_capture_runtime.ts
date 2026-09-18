/** Bind batch capture to the selected provider and existing maintenance fence. */
import type {JsonObject} from "../effect_program.ts";
import {requireJsonObject, requireBoolean} from "../runtime_decode.ts";
import {requireAuthorityStoreId} from "./authority_store_codec.ts";
import {registryAuthoritySourceCheck} from "./authority_source.ts";
import {openLocalAuthorityStore, localAuthorityOpenFailure, type LocalAuthorityProviderDependencies} from "./local_authority_provider.ts";
import {runtimeRoot, sourceAuthorityFor} from "./local_authority_runtime.ts";
import {withCanonicalWriter} from "./local_authority_write.ts";
import {ShadowManagementError} from "./shadow_management.ts";
import {COORDINATION_FOLLOWUP_CAPTURE_SCHEMA, COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA,
  executeCoordinationFollowupCapture} from "./todo_followup_capture.ts";

export async function captureLocalFollowups(value: unknown, dependencies: LocalAuthorityProviderDependencies = {}): Promise<JsonObject> {
  const evidence = {source_authority: "file_v0", decision_read_from_provider: true, legacy_fallback_used: false};
  try {
    const input = requireJsonObject(value, "follow-up capture request");
    if (input.schema_version !== COORDINATION_FOLLOWUP_CAPTURE_SCHEMA) throw new Error("follow-up capture request schema mismatch");
    for (const field of Object.keys(input)) {
      if (!["schema_version", "runtime_root", "goal_id", "operation_id", "intent", "dry_run", "registry_source"].includes(field)) throw new Error(`unsupported capture request field: ${field}`);
    }
    const root = runtimeRoot(input.runtime_root), goal = requireAuthorityStoreId(input.goal_id, "goal id");
    const dryRun = requireBoolean(input.dry_run, "dry_run");
    const operation = requireAuthorityStoreId(input.operation_id, "operation id");
    const intent = requireJsonObject(input.intent, "capture intent");
    const current = registryAuthoritySourceCheck(input, true);
    return await withCanonicalWriter(root, goal, dryRun, async () => {
      const store = await openLocalAuthorityStore(root, goal, dependencies);
      evidence.source_authority = sourceAuthorityFor(store);
      return {...await executeCoordinationFollowupCapture(store, {goal_id: goal, operation_id: operation,
        intent, dry_run: dryRun, now: new Date()}, current), ...evidence};
    });
  } catch (error) {
    return {schema_version: COORDINATION_FOLLOWUP_CAPTURE_RESULT_SCHEMA, status: "failed", changed: false,
      reason_code: error instanceof ShadowManagementError ? error.reason_code : "followup_capture_unavailable",
      reason: error instanceof Error ? error.message : String(error), ...evidence, ...localAuthorityOpenFailure(error)};
  }
}
