"""Typed Chat actions for confirming a steward team plan.

A team plan is the one typed action that commits a *set* of lanes, so it owns
two things the general action service does not: the facts a confirmation was
reviewed against, and what happens when that confirmation could only be applied
partly. Both live here so `chat_actions` stays the router rather than the
settlement.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .agent_registry import registered_agent_ids_for_goal
from .control_plane.runtime.time import now_utc_iso


def _team_plan_gap_lane_ids(
    plan: Mapping[str, Any], settled: Mapping[str, Any]
) -> list[str]:
    """Name the confirmed lanes a settlement did not staff.

    The lanes that stayed unstaffed are the plan's own lanes minus the ones the
    settlement reported, read here rather than carried as a second list, so a
    cursor cannot disagree with the plan about which lane is missing.
    """

    settled_lane_ids = {
        str(item.get("lane_id") or "")
        for item in settled.get("lane_settlements") or []
        if isinstance(item, Mapping)
    }
    return [
        str(lane.get("lane_id") or "")
        for lane in (plan.get("lanes") or [])
        if isinstance(lane, Mapping)
        and str(lane.get("lane_id") or "")
        and str(lane.get("lane_id") or "") not in settled_lane_ids
    ]




class ChatTeamPlanActionMixin:
    """Keep team-plan settlement and recovery out of the action router."""

    def _team_plan_state_fingerprint(
        self, goal_id: str, plan: Mapping[str, Any]
    ) -> str:
        from .chat_actions import _digest

        """Bind every fact a confirmed team plan was reviewed against.

        Registry bytes are not enough. A plan is reviewed against the Goal's own
        intent -- the objective its work advances -- and that intent lives in the
        active-state document and in the canonical source basis the lanes would
        be created against, neither of which the registry bytes cover. Changing
        the objective therefore used to leave the confirmed plan applicable,
        because nothing the preview bound had moved.

        An unreadable fact is bound as its own explicit absence rather than
        dropped from the digest, so the precondition fails closed in both
        directions: a Goal whose intent becomes readable after the preview asks
        the owner to confirm again instead of silently dropping the check.
        """

        from .control_plane.work_items.governed_transition_proposal import (
            steward_team_plan_intent_basis,
        )

        from .control_plane.work_items.governed_transition_proposal import (
            steward_team_plan_intent_basis,
        )

        goal = self._goal(goal_id)
        project = Path(str(goal.get("repo") or "")).expanduser()
        state_file = Path(str(goal.get("state_file") or ""))
        if not state_file.is_absolute():
            state_file = project / state_file
        try:
            state_digest: str | None = hashlib.sha256(
                state_file.read_bytes()
            ).hexdigest()
        except OSError:
            state_digest = None
        return _digest(
            {
                "registry": self._registry_fingerprint(),
                "goal_id": goal_id,
                "active_state": state_digest,
                "intent_basis": steward_team_plan_intent_basis(
                    goal_id=goal_id,
                    goal=goal,
                    registry_path=self.registry_path,
                    plan=plan,
                ),
            }
        )

    def _apply_team_plan(
        self, proposal_id: str, proposal: dict[str, Any], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Create each ready lane's first bounded Todo through the Todo owner."""

        goal_id = str(parameters["goal_id"])
        plan = parameters.get("plan")
        if not isinstance(plan, Mapping):
            raise ValueError("team plan proposal is malformed")
        # The preview bound this Goal's registration facts, its active-state
        # intent and the canonical basis its lanes would advance; re-read them
        # here so a plan confirmed against one objective cannot become work
        # under another, and so a registry change still asks for confirmation.
        current_fingerprint = self._team_plan_state_fingerprint(goal_id, plan)
        if current_fingerprint != proposal.get("expected_state_fingerprint"):
            stale = self.store.apply(
                proposal_id,
                current_state_fingerprint=current_fingerprint,
                receipt={},
            )
            return {"proposal": stale, "turn": None}
        settled = self._settle_team_plan_lanes(
            proposal_id=proposal_id,
            goal_id=goal_id,
            plan=plan,
            requested_by=str(parameters.get("requested_by") or "owner"),
        )
        if settled.get("error") == "team_plan_no_staffable_lane":
            # Every lane stayed a gap, so this confirmation created nothing and
            # reused nothing. The old path wrote a receipt that reported
            # "lanes already present" with a verified projection and an empty
            # Todo id, which reads as success where the readback finds no work.
            # A confirmation that can only create nothing is recorded as the
            # typed failure it is, and the plan's lanes and reasons stay in the
            # card the owner confirmed.
            return {
                "proposal": self.store.mark_failed(
                    proposal_id,
                    error_code="team_plan_no_staffable_lane",
                    message=(
                        f"none of the plan's {settled['gap_count']} lane(s) can be "
                        "staffed by this host, so confirming it created no work"
                    ),
                ),
                "turn": None,
            }
        lane_failure = settled.get("lane_failure")
        if lane_failure:
            # A lane failed after earlier lanes were written. The plan did not
            # apply, so it is not reported as applied; the identities that do
            # exist are recorded with the failure so the retry reconciles
            # against them instead of creating a second copy of the same lane.
            return {
                "proposal": self.store.mark_failed(
                    proposal_id,
                    error_code="team_plan_lane_write_failed",
                    message=(
                        f"lane {lane_failure['lane_id']} could not be created; "
                        f"{len(settled['lane_todo_ids'])} lane Todo(s) from this "
                        "plan already exist"
                    ),
                    details={
                        "goal_id": goal_id,
                        "lane_todo_ids": [str(item) for item in settled["lane_todo_ids"]],
                        "lane_settlements": [
                            dict(item) for item in settled["lane_settlements"]
                        ],
                        "failed_lane_id": str(lane_failure["lane_id"]),
                        "failed_lane_reason_code": str(lane_failure["reason_code"]),
                    },
                ),
                "turn": None,
            }
        receipt = self._team_plan_receipt(
            proposal_id=proposal_id,
            goal_id=goal_id,
            settled=settled,
        )
        if settled["gap_count"]:
            # A partially committed plan keeps the facts a recovery has to
            # satisfy, so the lanes it could not create stay recoverable
            # instead of becoming a receipt detail nobody can act on.
            receipt["recovery_cursor"] = self._team_plan_recovery_cursor(
                plan=plan,
                settled=settled,
            )
        stored = self.store.apply(
            proposal_id, current_state_fingerprint=current_fingerprint, receipt=receipt
        )
        return {"proposal": stored, "turn": None}

    def _team_plan_staffing_gap_lane_ids(
        self, goal_id: str, plan: Mapping[str, Any]
    ) -> list[str]:
        """Read the host's staffing verdict for a plan without creating work.

        A recovery has to decide whether it may act before the settlement
        creates anything, so the validation the settlement performs is read here
        as a verdict only: which of the plan's lanes this host cannot staff now.
        """

        from .control_plane.todos.contract import (
            TODO_ACTION_KIND_ADVANCEMENT_VALUES,
        )
        from .control_plane.work_items.governed_transition_proposal import (
            validate_steward_team_plan_preview,
        )

        goal = self._goal(goal_id)
        verdict = validate_steward_team_plan_preview(
            plan,
            registered_agent_ids=registered_agent_ids_for_goal(goal),
            supported_action_kinds=sorted(TODO_ACTION_KIND_ADVANCEMENT_VALUES),
        )
        return [
            str(lane.get("lane_id") or "")
            for lane in verdict["lanes"]
            if str(lane.get("staffing") or "") != "ready"
        ]

    def _settle_team_plan_lanes(
        self,
        *,
        proposal_id: str,
        goal_id: str,
        plan: Mapping[str, Any],
        requested_by: str,
    ) -> dict[str, Any]:
        """Ask the governed transition owner to ensure the plan's lane Todos.

        The settlement re-validates the plan against the host's own facts every
        time it runs, so both the first apply and a recovery go through this one
        call: a lane whose Todo already exists comes back as a reuse, and a lane
        this host still cannot staff comes back as a gap.
        """

        from .control_plane.work_items.governed_transition_proposal import (
            GovernedTransitionSettlementPhase,
            settle_governed_transition_proposals,
        )

        settlements = settle_governed_transition_proposals(
            registry_path=self.registry_path,
            goal_id=goal_id,
            agent_id=requested_by,
            effect_id=proposal_id,
            proposals=[{**dict(plan), "proposal_id": proposal_id}],
            existing_receipts=[],
            checkpoint=lambda _receipts: None,
            phase=GovernedTransitionSettlementPhase.PRE_SETTLEMENT,
        )
        settlement = settlements[0]
        lane_todo_ids = [str(item) for item in (settlement.get("lane_todo_ids") or [])]
        lane_settlements = [
            dict(item) for item in (settlement.get("lane_settlements") or [])
        ]
        return {
            "error": "" if lane_todo_ids else "team_plan_no_staffable_lane",
            "action": str(settlement.get("action") or ""),
            "todo_id": str(settlement.get("todo_id") or ""),
            "lane_todo_ids": lane_todo_ids,
            # A lane write that fails after earlier lanes exist is reported by
            # the settlement owner rather than raised, so the apply can record a
            # retry-safe failure that still names the identities it created.
            "lane_failure": settlement.get("lane_failure"),
            # A lane the settlement created is a lane this plan had not yet
            # committed; the settlement records that per lane, so the recovery
            # reads the same fact rather than re-deriving it from Todo ids.
            "created_lane_ids": [
                str(item.get("lane_id") or "")
                for item in lane_settlements
                if str(item.get("disposition") or "") == "created"
            ],
            "lane_settlements": lane_settlements,
            "intent_basis": str(settlement.get("intent_basis") or ""),
            "gap_count": int(settlement.get("gap_count") or 0),
        }

    def _team_plan_receipt(
        self,
        *,
        proposal_id: str,
        goal_id: str,
        settled: Mapping[str, Any],
    ) -> dict[str, Any]:
        from .chat_actions import _digest

        """Read what the settlement produced as the receipt the card shows."""

        lane_todo_ids = [str(item) for item in settled["lane_todo_ids"]]
        gap_count = int(settled["gap_count"])
        # The outcome is read from what the settlement actually produced, not
        # from "the action was not a creation": a plan that created lanes beside
        # a gap is a partial application, and reporting it as a full success
        # told the owner the commitment was kept when part of it was not.
        if str(settled["action"]) == "reused":
            outcome = "team_plan_lanes_already_present"
        elif gap_count:
            outcome = "team_plan_partially_applied"
        else:
            outcome = "team_plan_applied"
        receipt: dict[str, Any] = {
            "receipt_id": _digest(
                {
                    "proposal_id": proposal_id,
                    "goal_id": goal_id,
                    "lane_todo_ids": lane_todo_ids,
                }
            )[:32],
            "outcome": outcome,
            "projection_verified": True,
            "resource_ids": {
                "goal_id": goal_id,
                "todo_id": str(settled["todo_id"]),
                "lane_todo_ids": lane_todo_ids,
            },
        }
        if settled["lane_settlements"]:
            # Which lane each created Todo is, who runs it, the priority it
            # carries and the acceptance it was confirmed to end on, so the
            # owner's readback still names the commitment and not just the work.
            receipt["lanes"] = [dict(item) for item in settled["lane_settlements"]]
        if gap_count:
            receipt["gap_count"] = gap_count
        if settled["intent_basis"]:
            # The canonical revision these lanes were created against, so the
            # owner's readback can name what the work advances.
            receipt["intent_basis"] = str(settled["intent_basis"])
        return receipt

    def _team_plan_recovery_cursor(
        self,
        *,
        plan: Mapping[str, Any],
        settled: Mapping[str, Any],
    ) -> dict[str, Any]:
        from .chat_actions import _digest

        """Record what a later recovery of this plan still owes.

        A recovery finishes the lanes a confirmed plan could not staff, so the
        cursor carries the two facts that decision needs: the plan the owner
        confirmed (its digest, so a recovery can prove it is completing the same
        commitment) and the lanes that confirmation left unstaffed, named. The
        remaining lanes are derived from the plan itself rather than from a
        second list, so the cursor cannot disagree with the plan about which
        lane is missing.
        """

        return {
            "schema_version": "team_plan_recovery_cursor_v0",
            "plan_digest": _digest(dict(plan)),
            "gap_lane_ids": _team_plan_gap_lane_ids(plan, settled),
            "attempts": [],
        }

    def _record_team_plan_recovery_attempt(
        self,
        cursor: Mapping[str, Any],
        *,
        outcome: str,
        recovered_lane_ids: Mapping[str, Any] = (),
        unstaffable_lane_ids: Mapping[str, Any] = (),
        remaining_gap_lane_ids: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one bounded attempt to the cursor's own history."""

        attempts = [dict(item) for item in (cursor.get("attempts") or [])]
        attempts.append(
            {
                "outcome": str(outcome),
                "recovered_lane_ids": [str(item) for item in recovered_lane_ids],
                "unstaffable_lane_ids": [str(item) for item in unstaffable_lane_ids],
                "remaining_gap_lane_ids": (
                    [str(item) for item in (cursor.get("gap_lane_ids") or [])]
                    if remaining_gap_lane_ids is None
                    else [str(item) for item in remaining_gap_lane_ids]
                ),
                "recorded_at": now_utc_iso(),
            }
        )
        # A plan can only be recovered as often as it has lanes, so the history
        # stays a readback of what happened rather than an unbounded log.
        return {
            **dict(cursor),
            "gap_lane_ids": [
                str(item)
                for item in (
                    cursor.get("gap_lane_ids") or []
                    if remaining_gap_lane_ids is None
                    else remaining_gap_lane_ids
                )
            ],
            "attempts": attempts[-3:],
        }

    def _recover_team_plan(
        self, proposal_id: str, proposal: Mapping[str, Any]
    ) -> dict[str, Any]:
        from .chat_actions import _digest

        """Finish the lanes a confirmed plan left unstaffed.

        This is the re-entrant apply the roadmap's R1 exit names: the owner does
        not confirm the plan again, because the commitment is already theirs and
        the lanes are already recorded. Two things must hold, and both are read
        from the plan the owner actually confirmed:

        - the stored plan is still the plan that was confirmed (its digest), so
          a recovery can never complete a different commitment; and
        - the settlement still staffs every lane the plan already committed, so
          a recovery may only *add* the lanes that are staffable now, never
          quietly replace or drop one that already has a Todo.

        A refusal is recorded on the plan instead of being turned into a failure
        of an apply that already happened: the lanes that exist stay exactly as
        they are, and the cursor says what the attempt found.
        """

        parameters = proposal.get("normalized_parameters")
        parameters = parameters if isinstance(parameters, Mapping) else {}
        goal_id = str(parameters.get("goal_id") or "")
        plan = parameters.get("plan")
        if not goal_id or not isinstance(plan, Mapping):
            raise ValueError("team plan proposal is malformed")
        receipt = proposal.get("receipt")
        receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
        cursor = receipt.get("recovery_cursor")
        if not isinstance(cursor, Mapping) or not (cursor.get("gap_lane_ids") or []):
            raise ValueError("this team plan has no lane left to recover")
        if str(cursor.get("plan_digest") or "") != _digest(dict(plan)):
            # The stored plan is not the one this cursor was written for, so no
            # recovery can claim to be completing the confirmed commitment.
            receipt["recovery_cursor"] = self._record_team_plan_recovery_attempt(
                cursor, outcome="confirmed_plan_changed"
            )
            stored = self.store.record_team_plan_recovery(
                proposal_id, receipt=receipt
            )
            return {"proposal": stored, "turn": None}
        # The staffability verdict is read before the settlement runs, because
        # the settlement creates the lanes it finds staffable. A recovery that
        # would strand a lane the plan already committed has to refuse *before*
        # it writes anything, not after.
        committed_lane_ids = [
            str(item.get("lane_id") or "")
            for item in (receipt.get("lanes") or [])
            if isinstance(item, Mapping)
        ]
        verdict_gaps = self._team_plan_staffing_gap_lane_ids(goal_id, plan)
        regressed = sorted(
            lane_id for lane_id in committed_lane_ids if lane_id in verdict_gaps
        )
        if regressed:
            # A lane the plan already committed is unstaffable on the host now.
            # Finishing the plan would commit work the host cannot run, so this
            # is a refusal to act rather than a partial success: the committed
            # lanes stand where they are, and the plan says which one the host
            # can no longer staff instead of leaving it to the next reader.
            receipt["recovery_cursor"] = self._record_team_plan_recovery_attempt(
                cursor,
                outcome="committed_lane_unstaffable",
                unstaffable_lane_ids=regressed,
                remaining_gap_lane_ids=verdict_gaps,
            )
            stored = self.store.record_team_plan_recovery(
                proposal_id, receipt=receipt
            )
            return {"proposal": stored, "turn": None}
        settled = self._settle_team_plan_lanes(
            proposal_id=proposal_id,
            goal_id=goal_id,
            plan=plan,
            requested_by=str(parameters.get("requested_by") or "owner"),
        )
        recovered = [
            lane_id
            for lane_id in settled["created_lane_ids"]
            if lane_id and lane_id not in committed_lane_ids
        ]
        unsettled_lane_ids = _team_plan_gap_lane_ids(plan, settled)
        merged = self._team_plan_receipt(
            proposal_id=proposal_id,
            goal_id=goal_id,
            settled=settled,
        )
        # The lanes this plan ensured before are still this plan's lanes, so the
        # recovery reports the committed set rather than only what it touched.
        previous_lanes = {
            str(item.get("lane_id") or ""): dict(item)
            for item in (receipt.get("lanes") or [])
            if isinstance(item, Mapping)
        }
        for item in merged.get("lanes") or []:
            previous_lanes[str(item.get("lane_id") or "")] = dict(item)
        if previous_lanes:
            merged["lanes"] = list(previous_lanes.values())
        merged["outcome"] = (
            "team_plan_partially_applied"
            if merged.get("gap_count")
            else "team_plan_applied"
        )
        merged["recovered_lane_ids"] = sorted(
            {
                *[
                    str(lane_id)
                    for attempt in (cursor.get("attempts") or [])
                    if isinstance(attempt, Mapping)
                    for lane_id in (attempt.get("recovered_lane_ids") or [])
                ],
                *recovered,
            }
        )
        merged["recovery_cursor"] = self._record_team_plan_recovery_attempt(
            cursor,
            outcome="recovered" if recovered else "no_progress",
            recovered_lane_ids=recovered,
            remaining_gap_lane_ids=unsettled_lane_ids,
        )
        stored = self.store.record_team_plan_recovery(proposal_id, receipt=merged)
        return {"proposal": stored, "turn": None}
