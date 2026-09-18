"""Durable Chat ingress receipts shared by provider steering and conversation modes."""
from __future__ import annotations

import os
from typing import Any
from .chat import require_matching_replay
from .file_lock import exclusive_file_lock


class ChatIngressStore:
    def create_ingress_receipt(
        self,
        session_id: str,
        *,
        client_ingress_id: str,
        mode: str,
        message: str,
    ) -> tuple[dict[str, Any], bool]:
        """Reserve one idempotent external ingress before provider delivery."""

        from .chat_store import _opaque_id, _read_json, _atomic_write_json, utc_now, CHAT_INGRESS_SCHEMA_VERSION

        if self.load_session(session_id) is None:
            raise KeyError("chat session was not found")
        path = self._ingress_path(session_id, client_ingress_id)
        with exclusive_file_lock(
            path,
            agent_id="loopx-chat",
            operation="create_chat_ingress_receipt",
        ):
            existing = _read_json(path)
            if existing.get("schema_version") == CHAT_INGRESS_SCHEMA_VERSION:
                require_matching_replay(
                    existing,
                    identity="client_ingress_id",
                    request={"mode": _opaque_id(mode, field="mode"), "message": str(message)},
                )
                return existing, False
            now = utc_now()
            payload = {
                "schema_version": CHAT_INGRESS_SCHEMA_VERSION,
                "client_ingress_id": _opaque_id(
                    client_ingress_id,
                    field="client_ingress_id",
                ),
                "session_id": session_id,
                "mode": _opaque_id(mode, field="mode"),
                "status": "pending",
                "message": str(message),
                "active_turn_id": None,
                "error_code": None,
                "created_at": now,
                "updated_at": now,
            }
            _atomic_write_json(path, payload)
            os.chmod(path, 0o600)
            return payload, True

    def update_ingress_receipt(
        self,
        session_id: str,
        client_ingress_id: str,
        **changes: Any,
    ) -> dict[str, Any]:
        from .chat_store import _read_json, _atomic_write_json, utc_now, CHAT_INGRESS_SCHEMA_VERSION

        path = self._ingress_path(session_id, client_ingress_id)
        with exclusive_file_lock(
            path,
            agent_id="loopx-chat",
            operation="update_chat_ingress_receipt",
        ):
            payload = _read_json(path)
            if payload.get("schema_version") != CHAT_INGRESS_SCHEMA_VERSION:
                raise KeyError("chat ingress receipt was not found")
            allowed = {"status", "active_turn_id", "error_code"}
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"unsupported chat ingress fields: {sorted(unknown)}")
            payload.update(changes)
            payload["updated_at"] = utc_now()
            _atomic_write_json(path, payload, preserve_mode=True)
            return payload

    def loopx_ingress(self, session_id: str) -> list[dict[str, Any]]:
        """Read the existing ingress ledger; no second queue or delivery truth."""
        from .chat_store import _read_json

        rows = [_read_json(path) for path in (self._session_dir(session_id) / "ingress").glob("*.json")]
        return sorted((row for row in rows if row.get("mode") in {"loopx_queue", "loopx_inbox"}),
                      key=lambda row: (row.get("created_at", ""), row.get("client_ingress_id", "")))
