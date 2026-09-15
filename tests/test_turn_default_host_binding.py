"""The default Turn host follows the operator's configured credential."""

from __future__ import annotations

import pytest

from loopx.cli import build_parser
from loopx.control_plane.turn_driver.host_binding import (
    HOST_WITHOUT_OPERATOR_CREDENTIAL,
    HOST_WITH_OPERATOR_CREDENTIAL,
    configured_operator_credential,
    resolve_default_turn_host,
)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, HOST_WITHOUT_OPERATOR_CREDENTIAL),
        ({"DEEPSEEK_API_KEY": "sk-operator"}, HOST_WITH_OPERATOR_CREDENTIAL),
        ({"DEEPSEEK_API_KEY": ""}, HOST_WITHOUT_OPERATOR_CREDENTIAL),
        ({"DEEPSEEK_API_KEY": "   "}, HOST_WITHOUT_OPERATOR_CREDENTIAL),
        (
            {"DEEPSEEK_BASE_URL": "https://example.invalid"},
            HOST_WITHOUT_OPERATOR_CREDENTIAL,
        ),
    ],
)
def test_default_host_follows_credential_presence(environ, expected):
    assert resolve_default_turn_host(environ) == expected


def test_configured_credential_names_the_env_var():
    assert (
        configured_operator_credential({"DEEPSEEK_API_KEY": "sk-operator"})
        == "DEEPSEEK_API_KEY"
    )
    assert configured_operator_credential({}) is None


def _turn_argv(command: str) -> list[str]:
    argv = ["turn", command, "--goal-id", "goal-x", "--agent-id", "agent-x"]
    if command == "run-once":
        argv.extend(["--project", "."])
    return argv


@pytest.mark.parametrize("command", ["plan", "run-once"])
def test_cli_defaults_to_dsh_when_the_operator_credential_is_configured(
    command, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-operator")

    args = build_parser().parse_args(_turn_argv(command))

    assert args.host == HOST_WITH_OPERATOR_CREDENTIAL


@pytest.mark.parametrize("command", ["plan", "run-once"])
def test_cli_defaults_to_codex_cli_without_an_operator_credential(
    command, monkeypatch
):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    args = build_parser().parse_args(_turn_argv(command))

    assert args.host == HOST_WITHOUT_OPERATOR_CREDENTIAL


def test_explicit_host_still_wins_over_the_credential_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-operator")

    args = build_parser().parse_args(
        [*_turn_argv("run-once"), "--host", "generic-cli"]
    )

    assert args.host == "generic-cli"


@pytest.mark.parametrize("command", ["plan", "run-once"])
def test_default_execution_mode_matches_the_resolved_default_host(
    command, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-operator")

    managed = build_parser().parse_args(_turn_argv(command))

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    individual = build_parser().parse_args(_turn_argv(command))

    # A managed host plans bounded headless Turns; pairing it with a visible
    # interactive mode would make the shipped default unusable. run-once only
    # ships the isolated-headless mode, so it keeps that mode either way.
    assert managed.host == HOST_WITH_OPERATOR_CREDENTIAL
    assert managed.execution_mode == "isolated-headless"
    assert individual.host == HOST_WITHOUT_OPERATOR_CREDENTIAL
    assert individual.execution_mode == (
        "interactive-visible" if command == "plan" else "isolated-headless"
    )
