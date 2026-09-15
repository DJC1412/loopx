"""Installed prompt lifecycle; execution policy stays in heartbeat-prompt.

The App API is the preferred interactive writer. The journaled store writer is
a qualified local compatibility adapter, not a public Codex storage API.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import tempfile
import tomllib
from typing import Any, Mapping
from types import SimpleNamespace

from .bootstrap_prompt import (
    HEARTBEAT_BOOTSTRAP,
    BOOTSTRAP_INSTRUCTION,
    host_bootstrap_binding,
    goal_bootstrap,
    render_heartbeat_bootstrap,
)

from loopx.upgrade import (
    codex_home, infer_agent_id_from_prompt, infer_goal_id_from_prompt,
    infer_available_capabilities_from_prompt, load_codex_app_automation_manifest,
)

SCHEMA = "loopx_automation_prompt_upgrade_v0"
PROMPT_BINDING_SCHEMA_VERSION = "codex_app_automation_prompt_binding_v0"
PROMPT_BINDING_ADOPT_ACTION = "adopt_managed_bootstrap"
PROMPT_BINDING_HOST_ACTION_CONTRACT = "codex_app_automation_prompt_adoption"
PROMPT_BINDING_SPEND_POLICY = "no_spend_for_automation_prompt_adoption"
BOOTSTRAP = HEARTBEAT_BOOTSTRAP
_LEGACY_BOOTSTRAP = "LoopX managed heartbeat bootstrap v1"
_LEGACY_INSTRUCTION = (
    "读取完整结果；仅 ok=true 时按本次 task_body 执行，不复用旧指令；"
    "失败或结果不完整则停止并报告，不执行任务或记账。"
)
_BOOTSTRAP_INSTRUCTION = BOOTSTRAP_INSTRUCTION


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def bootstrap_prompt(*, registry: Path, goal_id: str, agent_id: str,
                     runtime_root: str | None = None,
                     capabilities: list[str] | None = None,
                     cli_bin: str = "loopx") -> str:
    args = [cli_bin, "--format", "json", "--registry", str(registry.resolve())]
    if runtime_root:
        args += ["--runtime-root", str(Path(runtime_root).expanduser().resolve())]
    args += ["heartbeat-prompt", "--thin", "--codex-app", "--goal-id", goal_id,
             "--agent-id", agent_id]
    if cli_bin != "loopx":
        args += ["--cli-bin", cli_bin]
    for capability in capabilities or []:
        args += ["--available-capability", capability]
    return render_heartbeat_bootstrap(args)


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".loopx-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def bootstrap_binding(prompt: str) -> dict | None:
    if not prompt.startswith((BOOTSTRAP + "\n", _LEGACY_BOOTSTRAP + "\n")):
        return None
    try:
        command = prompt.split("```sh\n", 1)[1].split("\n```", 1)[0]
        tokens = shlex.split(command)
        if tokens[1:3] != ["--format", "json"]:
            return None
        values: dict[str, Any] = {"capabilities": []}
        flags = {"--registry": "registry", "--runtime-root": "runtime_root",
                 "--goal-id": "goal_id", "--agent-id": "agent_id", "--cli-bin": "cli_bin"}
        index = 3
        while index < len(tokens):
            token = tokens[index]
            if token in ("heartbeat-prompt", "--thin", "--codex-app"):
                index += 1
                continue
            if token == "--available-capability":
                values["capabilities"].append(tokens[index + 1])
            elif token in flags and flags[token] not in values:
                values[flags[token]] = tokens[index + 1]
            else:
                return None
            index += 2
        values["registry"] = Path(values["registry"])
        if tokens[0] != values.get("cli_bin", "loopx"):
            return None
        expected = bootstrap_prompt(**values)
        legacy = expected.replace(BOOTSTRAP, _LEGACY_BOOTSTRAP, 1).removesuffix(
            _BOOTSTRAP_INSTRUCTION
        ) + _LEGACY_INSTRUCTION
        return values if prompt in (expected, legacy) else None
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _replace_prompt(source: str, prompt: str) -> str:
    """Retain unknown TOML fields/comments; prove only prompt changed by parsing.

    Candidate spans are not trusted lexically: multiline strings can contain
    fake assignments. Both prefix and full-document semantic checks must pass.
    """
    original = tomllib.loads(source)
    desired = {**original, "prompt": prompt}
    lines = source.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if not re.match(r"^prompt\s*=", line):
            continue
        try:
            prefix = tomllib.loads("".join(lines[:start]))
        except tomllib.TOMLDecodeError:
            continue
        if "prompt" in prefix:
            continue
        for end in range(start + 1, len(lines) + 1):
            candidate = "".join(lines[:start]) + "prompt = " + json.dumps(prompt, ensure_ascii=False) + "\n" + "".join(lines[end:])
            try:
                if tomllib.loads(candidate) == desired:
                    return candidate
            except tomllib.TOMLDecodeError:
                pass
    raise ValueError("unsupported automation TOML prompt encoding; use the App API")


def _connect(home: Path, *, writable: bool = False) -> sqlite3.Connection:
    path = home / "sqlite/codex-dev.db"
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    columns = {row[1] for row in connection.execute("PRAGMA table_info(automations)")}
    if not {"id", "kind", "prompt", "status", "target_thread_id"} <= columns:
        connection.close()
        raise ValueError("unsupported Codex automation schema; use the App API")
    return connection


def _read(home: Path, automation_id: str, connection: sqlite3.Connection) -> tuple[str, dict, dict]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", automation_id):
        raise ValueError("invalid automation id")
    path = home / "automations" / automation_id / "automation.toml"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("symlink automation stores are not supported")
    source = path.read_text(encoding="utf-8")
    if len(source.encode("utf-8")) > 256_000:
        raise ValueError("automation manifest exceeds the supported offline size")
    item = tomllib.loads(source)
    row = connection.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone()
    if row is None or item.get("id") != automation_id:
        raise ValueError("automation identity missing or mismatched")
    row = dict(row)
    if item.get("kind") != "heartbeat" or row["kind"] != "heartbeat":
        raise ValueError(
            "heartbeat kind/binding is not confirmed by both stores; reconcile through "
            "the App before prompt adoption (do not convert a cron row or infer its thread)"
        )
    if item.get("status") == "DELETED" or row["status"] == "DELETED":
        raise ValueError("deleted automation cannot be upgraded")
    for key in ("prompt", "status", "target_thread_id", "rrule"):
        if item.get(key) != row.get(key):
            raise ValueError(f"automation stores disagree on {key}; reconcile through the App")
    return source, item, row


def _desired_prompt(*, registry: Path, prompt: str, goal_id: str, agent_id: str,
                    runtime_root: str | None, cli_bin: str) -> str:
    """Desired installed body: keep an exact loader's own binding, else the loader."""
    desired = bootstrap_prompt(registry=registry, goal_id=goal_id, agent_id=agent_id,
        runtime_root=runtime_root, capabilities=infer_available_capabilities_from_prompt(prompt),
        cli_bin=cli_bin)
    loaded_binding = host_bootstrap_binding(prompt)
    if (loaded_binding and loaded_binding["registry"].resolve() == registry.resolve()
            and (loaded_binding.get("codex_app") or
                 loaded_binding.get("runtime_profile") == "codex_app_heartbeat")):
        # Upgrade the wrapper while retaining explicit owner
        # policy and scheduler inputs from the exact loader.
        desired = goal_bootstrap(SimpleNamespace(**loaded_binding), registry=registry)
    return desired


def _classify_entry(*, home: Path, connection: sqlite3.Connection, automation_id: str,
                    goals: dict[str, Any], registry: Path, runtime_root: str | None,
                    cli_bin: str) -> dict[str, Any]:
    """Classify one installed automation; shared by the batch plan and one-lane reads."""
    from loopx.agent_registry import registered_agent_ids_for_goal

    entry: dict[str, Any] = {"automation_id": automation_id}
    try:
        source, item, row = _read(home, automation_id, connection)
        prompt = item["prompt"]
        goal_id = infer_goal_id_from_prompt(prompt)
        agent_id = infer_agent_id_from_prompt(prompt)
        goal_mentions = set(re.findall(r"--goal-id\s+([A-Za-z0-9_.:-]+)", prompt))
        agent_mentions = set(re.findall(r"--agent-id\s+([A-Za-z0-9_.:-]+)", prompt))
        if (goal_mentions - {goal_id}) or (agent_mentions - {agent_id}):
            raise ValueError("ambiguous Goal/agent bindings; select and migrate through the App")
        if goal_id not in goals or agent_id not in registered_agent_ids_for_goal(goals[goal_id]):
            entry.update(status="unmanaged", reason="no unique registered Goal/agent binding")
        else:
            desired = _desired_prompt(registry=registry, prompt=prompt, goal_id=goal_id,
                agent_id=agent_id, runtime_root=runtime_root, cli_bin=cli_bin)
            entry.update(status="current" if prompt == desired else "adoption_required",
                goal_id=goal_id, agent_id=agent_id, prompt_sha256=digest(prompt),
                current_prompt=prompt,
                source_sha256=digest(source), desired_prompt=desired,
                desired_sha256=digest(desired), target_thread_id=row["target_thread_id"])
    except (ValueError, OSError) as error:
        entry.update(status="blocked", reason=str(error))
    return entry


def _registry_goals(registry: Path) -> dict[str, Any]:
    from loopx.history import load_registry
    from loopx.registry import registry_goals

    return {str(goal["id"]): goal for goal in registry_goals(load_registry(registry))}


def build_plan(*, registry: Path, home: Path | None = None,
               runtime_root: str | None = None, cli_bin: str = "loopx") -> dict[str, Any]:
    home = (home or codex_home()).expanduser().resolve()
    goals = _registry_goals(registry)
    entries = []
    with closing(_connect(home)) as connection:
        for path in sorted((home / "automations").glob("*/automation.toml")):
            entries.append(_classify_entry(home=home, connection=connection,
                automation_id=path.parent.name, goals=goals, registry=registry,
                runtime_root=runtime_root, cli_bin=cli_bin))
    return {"schema_version": SCHEMA, "ok": True, "codex_home": str(home),
            "entries": entries, "writes": False,
            "policy": "Discovery is not adoption authority. Review each replacement; use the App API first."}


def automation_update_request(*, automation_id: str, manifest: Mapping[str, Any],
                              expected_prompt_sha256: str, desired_prompt: str) -> dict[str, Any]:
    """Complete prompt-only App request; scheduling and thread binding are preserved.

    The CLI cannot call an in-App tool itself, so every entrypoint that finds a
    stale installed body hands the host this exact reviewed request.
    """
    return {"tool": "automation_update",
            "expected_prompt_sha256": expected_prompt_sha256,
            "precondition": "View the same automation; verify this prompt hash and all "
                "preserved fields before update; read back afterward.",
            "arguments": {"mode": "update", "id": automation_id, "kind": "heartbeat",
                "name": manifest["name"], "status": manifest["status"],
                "rrule": manifest["rrule"],
                "targetThreadId": manifest["target_thread_id"],
                "notificationPolicy": manifest.get("notification_policy"),
                "prompt": desired_prompt}}


def installed_prompt_binding(*, registry: Path, goal_id: str, agent_id: str,
                             home: Path | None = None, thread_id: str | None = None,
                             runtime_root: str | None = None,
                             cli_bin: str = "loopx") -> dict[str, Any]:
    """Read-only: is the automation driving this lane the current managed loader?

    A frozen execution body keeps applying the policy of the day it was installed
    to every later wake, and update-time reconciliation reports that only once, so
    the live turn contract has to carry the observation. Bounded and fail-open: an
    unreadable store reports a status instead of failing the turn.

    Several installed automations can claim one Goal/agent, so discovery is
    TOML-only and the automation bound to the current thread wins. Only a body both
    stores confirm is classified: a cron row or an unbound look-alike cannot
    describe this lane.
    """
    home = (home or codex_home()).expanduser().resolve()
    observed: dict[str, Any] = {"schema_version": PROMPT_BINDING_SCHEMA_VERSION,
        "status": "absent", "automation_id": None, "host_action": "none",
        "host_action_contract": "none", "spend_policy": PROMPT_BINDING_SPEND_POLICY}
    installed = load_codex_app_automation_manifest(home)
    if not installed.get("available"):
        return {**observed, "status": "unavailable", "reason": str(installed.get("reason")
            or "no installed Codex App automation store")[:200]}
    lane = [item for item in installed.get("entries") or [] if isinstance(item, dict)
            and item.get("installed") is True and item.get("goal_id") == goal_id
            and item.get("agent_id") == agent_id]
    if not lane:
        return observed
    thread_id = str(thread_id or "").strip()
    bound = {str(item.get("automation_id")) for item in lane if thread_id
             and str(item.get("target_thread_id") or "") == thread_id}
    unresolved: list[str] = []
    confirmed: list[tuple[str, str]] = []
    try:
        with closing(_connect(home)) as connection:
            for item in lane:
                automation_id = str(item.get("automation_id") or "")
                try:
                    _, installed_item, _ = _read(home, automation_id, connection)
                except ValueError:
                    unresolved.append(automation_id)
                    continue
                confirmed.append((automation_id, installed_item["prompt"]))
            chosen = [pair for pair in confirmed if pair[0] in bound] or confirmed
            if len(chosen) > 1:
                return {**observed, "status": "ambiguous", "goal_id": goal_id,
                        "agent_id": agent_id,
                        "automation_ids": sorted(pair[0] for pair in chosen)[:8],
                        "reason": "several installed automations claim this Goal/agent; "
                            "reconcile through the App"}
            entry = (_installed_body_entry(chosen[0][1], home=home, connection=connection,
                        automation_id=chosen[0][0], registry=registry,
                        goal_id=goal_id, agent_id=agent_id, runtime_root=runtime_root,
                        cli_bin=cli_bin) if chosen else None)
    except (OSError, ValueError, sqlite3.Error) as error:
        return {**observed, "status": "unavailable", "goal_id": goal_id, "agent_id": agent_id,
                "reason": str(error)[:200]}
    if entry is None:
        return {**observed, "status": "blocked", "goal_id": goal_id, "agent_id": agent_id,
                "unresolved_automation_ids": sorted(unresolved)[:8],
                "reason": "no installed automation for this Goal/agent is confirmed by both "
                    "stores; reconcile through the App"}
    status = str(entry.get("status") or "unknown")
    if status not in {"current", "adoption_required"}:
        # An unmanaged or unconfirmable body is not adoption authority, so it
        # reports its own classification instead of a request the host must not
        # apply.
        return {**observed, "status": status, "automation_id": str(entry["automation_id"]),
                "goal_id": goal_id, "agent_id": agent_id, "reason": entry.get("reason")}
    result = {**observed, "status": status, "automation_id": str(entry["automation_id"]),
              "goal_id": goal_id, "agent_id": agent_id, "prompt_sha256": entry["prompt_sha256"],
              "desired_sha256": entry["desired_sha256"]}
    if status == "current":
        return result
    try:
        manifest = tomllib.loads((home / "automations" / str(entry["automation_id"])
            / "automation.toml").read_text(encoding="utf-8"))
        if {"name", "status", "rrule", "target_thread_id"} - manifest.keys():
            raise ValueError("installed automation is missing fields the App request must preserve")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
        return {**result, "status": "blocked", "reason": str(error)[:200]}
    return {**result, "automation_status": str(manifest.get("status") or ""),
            "host_action": PROMPT_BINDING_ADOPT_ACTION,
            "host_action_contract": PROMPT_BINDING_HOST_ACTION_CONTRACT,
            "reason": "the installed automation body is not the current managed loader; "
                "review this prompt-only request through the App",
            "api_update_request": automation_update_request(
                automation_id=str(entry["automation_id"]), manifest=manifest,
                expected_prompt_sha256=entry["prompt_sha256"],
                desired_prompt=entry["desired_prompt"])}


def _installed_body_entry(prompt: str, *, home: Path, connection: sqlite3.Connection,
                          automation_id: str, registry: Path, goal_id: str, agent_id: str,
                          runtime_root: str | None, cli_bin: str) -> dict[str, Any]:
    """Classify one confirmed body; an exact loader settles its own binding.

    A turn must never retarget another Codex home, registry, or CLI binary, so a
    recognized loader is reviewed against the registry it already names; only an
    unrecognized body is reviewed against the caller's registry.
    """
    loaded = host_bootstrap_binding(prompt)
    if loaded is None:
        return _classify_entry(home=home, connection=connection, automation_id=automation_id,
            goals=_registry_goals(registry), registry=registry, runtime_root=runtime_root,
            cli_bin=cli_bin)
    desired = _desired_prompt(registry=loaded["registry"], prompt=prompt, goal_id=goal_id,
        agent_id=agent_id, runtime_root=runtime_root, cli_bin=cli_bin)
    return {"automation_id": automation_id,
            "status": "current" if prompt == desired else "adoption_required",
            "prompt_sha256": digest(prompt), "desired_prompt": desired,
            "desired_sha256": digest(desired)}


def apply_offline(*, home: Path, automation_id: str, expected_prompt_sha256: str,
                  desired_prompt: str, expected_source_sha256: str | None = None) -> dict[str, Any]:
    """Journaled prompt-only write, also used by qualified update-time migration.

    Keep the SQLite writer lock through mirror replacement and readback. TOML
    cannot join that transaction: a crash is explicitly journal-recoverable,
    not falsely advertised as an atomic two-store commit. Concurrent external
    edits detected at either boundary fail instead of being retried blindly.
    The historical Python name is retained for the explicit offline CLI.
    """
    home = home.expanduser().resolve()
    journal = home / "loopx-automation-backups" / (automation_id + ".json")
    if journal.is_symlink() or journal.parent.is_symlink():
        raise ValueError("symlink backup stores are not supported")
    with closing(_connect(home, writable=True)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        source, item, row = _read(home, automation_id, connection)
        path = home / "automations" / automation_id / "automation.toml"
        if expected_source_sha256 is not None and digest(source) != expected_source_sha256:
            raise ValueError("automation metadata changed after preview; no writes performed")
        if digest(item["prompt"]) != expected_prompt_sha256:
            raise ValueError("prompt changed after preview; no writes performed")
        if item["prompt"] == desired_prompt:
            return {"ok": True, "status": "current", "automation_id": automation_id}
        if journal.exists():
            raise ValueError("previous migration journal exists; recover or rollback it first")
        replacement = _replace_prompt(source, desired_prompt)
        # Entire originals stay private for recovery; no raw prompts in receipts.
        _atomic(journal, json.dumps({"schema_version": SCHEMA, "automation_id": automation_id,
            "before": source, "after": replacement, "row": row}, ensure_ascii=False))
        if path.read_text(encoding="utf-8") != source:
            raise ValueError("automation manifest changed during migration; journal retained")
        changed = connection.execute("UPDATE automations SET prompt=? WHERE id=? AND prompt=?",
                                     (desired_prompt, automation_id, item["prompt"]))
        if changed.rowcount != 1:
            raise ValueError("automation prompt compare-and-swap failed")
        _atomic(path, replacement)
        if path.read_text(encoding="utf-8") != replacement:
            raise ValueError("automation manifest changed during readback; journal retained")
        _read(home, automation_id, connection)
        connection.commit()
    with closing(_connect(home)) as connection:
        final_source, _, final_row = _read(home, automation_id, connection)
        if final_source != replacement or final_row["prompt"] != desired_prompt:
            raise ValueError("automation changed after commit; inspect the retained journal")
    return {"ok": True, "status": "updated", "automation_id": automation_id,
            "backup": str(journal), "future_policy": "read installed heartbeat-prompt on every wake"}


def recover_offline(*, home: Path, automation_id: str, rollback: bool = False) -> dict:
    home = home.expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", automation_id):
        raise ValueError("invalid automation id")
    journal = home / "loopx-automation-backups" / (automation_id + ".json")
    if journal.is_symlink() or journal.parent.is_symlink():
        raise ValueError("symlink backup stores are not supported")
    saved = json.loads(journal.read_text(encoding="utf-8"))
    if saved.get("schema_version") != SCHEMA or saved.get("automation_id") != automation_id:
        raise ValueError("invalid migration journal")
    before, after = saved["before"], saved["after"]
    prompts = [tomllib.loads(text)["prompt"] for text in (before, after)]
    path = home / "automations" / automation_id / "automation.toml"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("symlink automation stores are not supported")
    with closing(_connect(home, writable=True)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone()
        if row is None or row["prompt"] not in prompts or path.read_text(encoding="utf-8") not in (before, after):
            raise ValueError("automation changed since migration; recovery refuses to overwrite it")
        original = saved["row"]
        if any(row[key] != value for key, value in original.items() if key != "prompt"):
            raise ValueError("automation metadata changed; reconcile through the App")
        selected = before if rollback else after
        connection.execute("UPDATE automations SET prompt=? WHERE id=?", (tomllib.loads(selected)["prompt"], automation_id))
        connection.commit()
    _atomic(path, selected)
    with closing(_connect(home)) as connection:
        _read(home, automation_id, connection)
    return {"ok": True, "status": "rolled_back" if rollback else "recovered", "automation_id": automation_id}
