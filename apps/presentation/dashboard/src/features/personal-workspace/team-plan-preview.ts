/**
 * Present a validated steward team plan for owner confirmation.
 *
 * The reducer owns no action authority: it reads the preview the host already
 * validated and shows, per lane, the Agent that runs it, that lane's first
 * bounded Todo, its acceptance signal, and whether the lane is a declared
 * staffing gap. Confirming the card is what asks the canonical owners to create
 * the lanes, so the card states what confirming does and never claims a lane
 * already exists.
 */

import type { WorkspaceTranslate } from "./i18n.js";

export type TeamPlanPreviewField = { key: string; label: string; value: string };

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function teamPlanFields(
  parameters: Record<string, unknown>,
  t: WorkspaceTranslate,
): TeamPlanPreviewField[] {
  const plan = asRecord(parameters.plan);
  const fields: TeamPlanPreviewField[] = [];
  const goalId = asText(parameters.goal_id) || asText(plan.goal_id);
  if (goalId) {
    fields.push({ key: "goal_id", label: t("proposal.field.goalId"), value: goalId });
  }
  const objective = asText(plan.objective);
  if (objective) {
    fields.push({ key: "objective", label: t("proposal.field.objective"), value: objective });
  }
  const lanes = Array.isArray(plan.lanes) ? plan.lanes : [];
  lanes.forEach((rawLane, index) => {
    const lane = asRecord(rawLane);
    const laneId = asText(lane.lane_id) || `lane-${index + 1}`;
    const agentId = asText(lane.agent_id);
    const acceptance = asText(lane.acceptance);
    if (asText(lane.staffing) === "gap") {
      // A gap lane keeps the work it did not staff and creates nothing.
      const declined = asRecord(lane.declined_first_todo);
      fields.push({
        key: `lane_${laneId}`,
        label: agentId || laneId,
        value: [
          t("proposal.teamPlan.gapLane"),
          asText(lane.gap_reason_code),
          asText(declined.text),
        ].filter(Boolean).join(" · "),
      });
      return;
    }
    const todo = asRecord(lane.first_todo);
    const laneValue = [
      asText(todo.priority),
      asText(todo.action_kind),
      asText(todo.text),
    ].filter(Boolean).join(" · ");
    fields.push({
      key: `lane_${laneId}`,
      label: agentId || laneId,
      value: [
        laneValue || t("proposal.teamPlan.laneUnstaffed"),
        acceptance ? `${t("proposal.teamPlan.acceptanceShort")}: ${acceptance}` : "",
      ].filter(Boolean).join(" · "),
    });
  });
  const gaps = Array.isArray(plan.gaps) ? plan.gaps : [];
  if (gaps.length > 0) {
    fields.push({
      key: "lane_gaps",
      label: t("proposal.field.laneGaps"),
      value: gaps
        .map((rawGap) => {
          const gap = asRecord(rawGap);
          return [asText(gap.lane_id), asText(gap.reason_code)].filter(Boolean).join(": ");
        })
        .filter(Boolean)
        .join(" · "),
    });
  }
  const envelope = asRecord(plan.quota_envelope);
  const envelopeEntries = Object.entries(envelope);
  if (envelopeEntries.length > 0) {
    fields.push({
      key: "quota_envelope",
      label: t("proposal.field.quotaEnvelope"),
      value: envelopeEntries.map(([key, value]) => `${key}: ${String(value ?? "")}`).join(" · "),
    });
  }
  const stopCondition = asText(plan.stop_condition);
  if (stopCondition) {
    fields.push({
      key: "stop_condition",
      label: t("proposal.field.stopCondition"),
      value: stopCondition,
    });
  }
  return fields;
}

export function teamPlanLaneCount(parameters: Record<string, unknown>): number {
  const plan = asRecord(parameters.plan);
  return Array.isArray(plan.lanes) ? plan.lanes.length : 0;
}

export function teamPlanGoalId(parameters: Record<string, unknown>): string {
  const plan = asRecord(parameters.plan);
  return asText(parameters.goal_id) || asText(plan.goal_id);
}

/**
 * What confirming a plan actually did, read from the receipt the apply wrote.
 *
 * A confirmation the owner was shown as one commitment can create every lane,
 * some of them, or find them already present. The receipt is the only surface
 * that knows which happened, so the card reads it instead of reporting every
 * applied plan as the same success.
 */
export type TeamPlanAppliedOutcome = {
  kind: "applied" | "partially_applied" | "already_present";
  created: number;
  gaps: number;
};

export function teamPlanAppliedOutcome(receipt: unknown): TeamPlanAppliedOutcome | null {
  const record = asRecord(receipt);
  const outcome = asText(record.outcome);
  if (!outcome.startsWith("team_plan_")) return null;
  const lanes = Array.isArray(record.lanes) ? record.lanes.length : 0;
  const gaps = typeof record.gap_count === "number" ? record.gap_count : 0;
  if (outcome === "team_plan_partially_applied") {
    return { kind: "partially_applied", created: lanes, gaps };
  }
  if (outcome === "team_plan_lanes_already_present") {
    return { kind: "already_present", created: lanes, gaps };
  }
  if (outcome === "team_plan_applied") {
    return { kind: "applied", created: lanes, gaps };
  }
  // A plan whose receipt the apply recorded as a typed failure is not an applied
  // outcome, so the card keeps the failure it already renders.
  return null;
}

/**
 * The line an applied plan shows, or the surface's own applied sentence.
 *
 * A partial application names how many lanes exist and how many were left
 * unstaffed, because "applied" alone told the owner that a commitment was kept
 * when part of it was not.
 */
export function teamPlanAppliedLine(
  outcome: TeamPlanAppliedOutcome | null,
  t: WorkspaceTranslate,
): string {
  if (outcome?.kind === "partially_applied") {
    return t("proposal.teamPlan.appliedPartially", {
      created: String(outcome.created),
      gaps: String(outcome.gaps),
    });
  }
  if (outcome?.kind === "already_present") {
    return t("proposal.teamPlan.appliedAlreadyPresent");
  }
  return t("drawer.proposalApplied");
}
