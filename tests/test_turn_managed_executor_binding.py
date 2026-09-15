"""The managed executor readback names the executor and whether it can launch."""

from __future__ import annotations

import pytest

from loopx.control_plane.turn_driver.host_binding import (
    DSH_RUNTIME_UNAVAILABLE,
    EXECUTOR_KIND_GENERIC,
    EXECUTOR_KIND_INDIVIDUAL,
    EXECUTOR_KIND_MANAGED,
    MANAGED_EXECUTOR_BINDING_SCHEMA_VERSION,
    configured_operator_credential,
    managed_executor_binding,
    resolve_default_turn_host,
)

_NO_RUNTIME = lambda _module: False  # noqa: E731 - tiny probe fixture
_RUNTIME = lambda _module: True  # noqa: E731 - tiny probe fixture


def test_managed_executor_reports_the_operator_credential_and_endpoint():
    binding = managed_executor_binding(
        "dsh",
        environ={
            "DEEPSEEK_API_KEY": "sk-operator",
            "DEEPSEEK_BASE_URL": "https://example.invalid",
        },
        module_probe=_RUNTIME,
    )

    assert binding == {
        "schema_version": MANAGED_EXECUTOR_BINDING_SCHEMA_VERSION,
        "executor": "dsh",
        "executor_kind": EXECUTOR_KIND_MANAGED,
        "credential_env": "DEEPSEEK_API_KEY",
        "endpoint_env": "DEEPSEEK_BASE_URL",
        "operator_credential_bound": True,
        "available": True,
        "unavailable_reason": None,
    }


def test_managed_executor_fails_closed_when_the_runtime_is_missing():
    binding = managed_executor_binding(
        "dsh",
        environ={"DEEPSEEK_API_KEY": "sk-operator"},
        module_probe=_NO_RUNTIME,
    )

    assert binding["available"] is False
    assert binding["unavailable_reason"] == DSH_RUNTIME_UNAVAILABLE


def test_configured_runner_hook_makes_the_managed_host_launchable():
    binding = managed_executor_binding(
        "dsh",
        environ={"DEEPSEEK_API_KEY": "sk-operator"},
        dsh_runner_configured=True,
        module_probe=_NO_RUNTIME,
    )

    assert binding["available"] is True
    assert binding["unavailable_reason"] is None


def test_managed_executor_reports_an_unconfigured_credential_without_inventing_one():
    binding = managed_executor_binding("dsh", environ={}, module_probe=_RUNTIME)

    assert binding["credential_env"] is None
    assert binding["endpoint_env"] is None


def test_unbound_managed_selection_states_the_credential_boundary():
    """An explicit dsh host without a credential must not claim to be bound."""

    binding = managed_executor_binding("dsh", environ={}, module_probe=_RUNTIME)

    assert binding["executor_kind"] == EXECUTOR_KIND_MANAGED
    assert binding["operator_credential_bound"] is False
    assert binding["available"] is True


def test_configured_runner_hook_counts_as_an_operator_credential_boundary():
    binding = managed_executor_binding(
        "dsh",
        environ={},
        dsh_runner_configured=True,
        module_probe=_NO_RUNTIME,
    )

    assert binding["operator_credential_bound"] is True


def test_individual_and_generic_executors_are_not_operator_credential_bound():
    for host in ("codex-cli", "generic-cli"):
        binding = managed_executor_binding(host, environ={"DEEPSEEK_API_KEY": "sk-x"})

        assert binding["operator_credential_bound"] is False, binding


@pytest.mark.parametrize(
    ("host", "expected_kind"),
    [
        ("codex-cli", EXECUTOR_KIND_INDIVIDUAL),
        ("claude-code", EXECUTOR_KIND_INDIVIDUAL),
        ("generic-cli", EXECUTOR_KIND_GENERIC),
    ],
)
def test_other_hosts_make_no_launch_claim_and_carry_no_operator_env(
    host, expected_kind
):
    binding = managed_executor_binding(
        host,
        environ={"DEEPSEEK_API_KEY": "sk-operator"},
        module_probe=_RUNTIME,
    )

    assert binding["executor_kind"] == expected_kind
    assert binding["available"] is None
    assert binding["unavailable_reason"] is None
    assert binding["credential_env"] is None
    assert binding["endpoint_env"] is None
    assert binding["operator_credential_bound"] is False


@pytest.mark.parametrize(
    "environ",
    [{}, {"DEEPSEEK_API_KEY": ""}, {"DEEPSEEK_API_KEY": "   "}],
)
def test_default_resolution_and_executor_kind_agree(environ):
    default_host = resolve_default_turn_host(environ)
    binding = managed_executor_binding(
        default_host,
        environ=environ,
        module_probe=_RUNTIME,
    )

    # A machine with an operator credential resolves to the managed executor and
    # one without it resolves to the individual CLI host, so the readback never
    # claims managed execution the default would not actually select.
    expected_kind = (
        EXECUTOR_KIND_MANAGED
        if configured_operator_credential(environ)
        else EXECUTOR_KIND_INDIVIDUAL
    )
    assert binding["executor_kind"] == expected_kind


def test_endpoint_without_credential_is_reported_but_does_not_switch_host():
    environ = {"DEEPSEEK_BASE_URL": "https://example.invalid"}
    binding = managed_executor_binding("codex-cli", environ=environ)

    assert resolve_default_turn_host(environ) == "codex-cli"
    assert binding["endpoint_env"] is None
