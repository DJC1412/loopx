"""Credential-resolved default Turn host binding.

The default host for ``loopx turn plan`` and ``loopx turn run-once`` is decided
by what the operator configured, not by the harness alone:

- a configured operator model credential selects the DeepSeek Harness host
  (``dsh``), so the bounded Turn runs on the operator-supplied endpoint instead
  of any individual's CLI subscription;
- with no operator credential configured the default stays the Codex CLI host
  (``codex-cli``).

Resolution is a pure function of the environment so the command defaults, the
Turn plan readback, and tests quote one rule instead of drifting apart.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

HOST_WITH_OPERATOR_CREDENTIAL = "dsh"
HOST_WITHOUT_OPERATOR_CREDENTIAL = "codex-cli"

# Credentials the DSH Turn host already reads for its provider. Presence is the
# whole signal: the binding never invents a credential or falls back to a
# personal subscription when one is configured.
OPERATOR_CREDENTIAL_ENV_VARS = ("DEEPSEEK_API_KEY",)
OPERATOR_ENDPOINT_ENV_VAR = "DEEPSEEK_BASE_URL"


def configured_operator_credential(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return the configured operator credential env var name, else ``None``."""

    source = os.environ if environ is None else environ
    for name in OPERATOR_CREDENTIAL_ENV_VARS:
        if str(source.get(name, "") or "").strip():
            return name
    return None


def resolve_default_turn_host(environ: Mapping[str, str] | None = None) -> str:
    """Return the shipped default Turn host for this operator environment."""

    if configured_operator_credential(environ) is not None:
        return HOST_WITH_OPERATOR_CREDENTIAL
    return HOST_WITHOUT_OPERATOR_CREDENTIAL
