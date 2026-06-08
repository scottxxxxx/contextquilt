"""Unit tests for src/contextquilt/secrets.py.

Covers the get_secret() resolution order, ensure_secrets_in_env()
behavior with and without a GCP project, lru_cache memoization, and
graceful failure when the google-cloud-secret-manager package is
unavailable.

We never hit the real GCP API in these tests — the import-guarded
client construction is monkeypatched.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from contextquilt import secrets as secrets_module


@pytest.fixture(autouse=True)
def _clean_secrets_cache():
    """Each test starts with an empty get_secret cache."""
    secrets_module.get_secret.cache_clear()
    yield
    secrets_module.get_secret.cache_clear()


def _wipe_env(monkeypatch):
    # Both managed-key env vars + the project pointer. Listed by hand
    # rather than from _SECRET_MANAGER_MAPPINGS so adding a future
    # mapping doesn't silently widen test-fixture wipes.
    for var in ("CQ_ANTHROPIC_API_KEY", "CQ_LLM_API_KEY", "CQ_GCP_PROJECT"):
        monkeypatch.delenv(var, raising=False)


# --- get_secret resolution order ---

def test_env_var_wins_over_secret_manager(monkeypatch):
    _wipe_env(monkeypatch)
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-from-env")
    # Even with a project configured, env must win.
    monkeypatch.setenv("CQ_GCP_PROJECT", "contextquilt")
    result = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert result == "sk-from-env"


def test_no_project_no_env_returns_empty(monkeypatch):
    _wipe_env(monkeypatch)
    result = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert result == ""


def test_sm_fallback_when_env_empty(monkeypatch):
    """When env is empty and a project is set, get_secret should fetch
    from SM. We monkeypatch the lazy import so no real network call
    happens."""
    _wipe_env(monkeypatch)
    monkeypatch.setenv("CQ_GCP_PROJECT", "contextquilt")

    # Build a fake `google.cloud.secretmanager` module that returns a
    # predictable payload. The lazy import inside get_secret picks it up.
    fake_response = MagicMock()
    fake_response.payload.data = b"sk-ant-from-sm"
    fake_client = MagicMock()
    fake_client.access_secret_version.return_value = fake_response
    fake_secretmanager = types.SimpleNamespace(
        SecretManagerServiceClient=lambda: fake_client
    )
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.secretmanager = fake_secretmanager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager)

    result = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert result == "sk-ant-from-sm"
    fake_client.access_secret_version.assert_called_once()
    call_args = fake_client.access_secret_version.call_args
    assert (
        call_args.kwargs["request"]["name"]
        == "projects/contextquilt/secrets/anthropic-api-key/versions/latest"
    )


def test_sm_failure_returns_empty(monkeypatch):
    _wipe_env(monkeypatch)
    monkeypatch.setenv("CQ_GCP_PROJECT", "contextquilt")

    fake_client = MagicMock()
    fake_client.access_secret_version.side_effect = RuntimeError("403 forbidden")
    fake_secretmanager = types.SimpleNamespace(
        SecretManagerServiceClient=lambda: fake_client
    )
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.secretmanager = fake_secretmanager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager)

    result = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert result == ""


def test_missing_sdk_returns_empty(monkeypatch):
    """When google-cloud-secret-manager isn't installed, get_secret must
    still return "" (degraded but functional)."""
    _wipe_env(monkeypatch)
    monkeypatch.setenv("CQ_GCP_PROJECT", "contextquilt")

    # Force the import to fail by inserting a sentinel that raises.
    class _BoomModule:
        def __getattr__(self, name):
            raise ImportError("simulated missing dep")

    # Easiest way: remove any cached module so the lazy import inside
    # get_secret re-resolves and fails on the actual import path.
    monkeypatch.setitem(sys.modules, "google.cloud", None)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", None)

    result = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert result == ""


# --- ensure_secrets_in_env ---

def test_ensure_secrets_no_project_is_noop(monkeypatch):
    _wipe_env(monkeypatch)
    # Should NOT raise, NOT touch env.
    secrets_module.ensure_secrets_in_env()
    import os
    assert "CQ_ANTHROPIC_API_KEY" not in os.environ


def test_ensure_secrets_skips_when_env_already_set(monkeypatch):
    """If the env var is already populated, the SM round-trip is skipped
    entirely (local dev / operator override wins). Both managed keys
    are set so the loop doesn't fall through to SM for the other one."""
    _wipe_env(monkeypatch)
    monkeypatch.setenv("CQ_ANTHROPIC_API_KEY", "sk-local-dev")
    monkeypatch.setenv("CQ_LLM_API_KEY", "sk-or-local-dev")
    monkeypatch.setenv("CQ_GCP_PROJECT", "contextquilt")

    # Set up a tripwire — if SM is actually hit, the test fails loudly.
    fake_client = MagicMock()
    fake_client.access_secret_version.side_effect = AssertionError(
        "SM should not be called when env var is set"
    )
    fake_secretmanager = types.SimpleNamespace(
        SecretManagerServiceClient=lambda: fake_client
    )
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.secretmanager = fake_secretmanager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager)

    secrets_module.ensure_secrets_in_env()
    fake_client.access_secret_version.assert_not_called()


def test_ensure_secrets_populates_from_sm(monkeypatch):
    _wipe_env(monkeypatch)
    monkeypatch.setenv("CQ_GCP_PROJECT", "contextquilt")

    fake_response = MagicMock()
    fake_response.payload.data = b"sk-ant-bootstrap"
    fake_client = MagicMock()
    fake_client.access_secret_version.return_value = fake_response
    fake_secretmanager = types.SimpleNamespace(
        SecretManagerServiceClient=lambda: fake_client
    )
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.secretmanager = fake_secretmanager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager)

    import os
    secrets_module.ensure_secrets_in_env()
    assert os.environ.get("CQ_ANTHROPIC_API_KEY") == "sk-ant-bootstrap"


# --- lru_cache memoization ---

def test_get_secret_lru_cache_dedupes_calls(monkeypatch):
    """A second call with the same args returns the cached value without
    re-hitting SM."""
    _wipe_env(monkeypatch)
    monkeypatch.setenv("CQ_GCP_PROJECT", "contextquilt")

    fake_response = MagicMock()
    fake_response.payload.data = b"sk-cached"
    fake_client = MagicMock()
    fake_client.access_secret_version.return_value = fake_response
    fake_secretmanager = types.SimpleNamespace(
        SecretManagerServiceClient=lambda: fake_client
    )
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.secretmanager = fake_secretmanager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager)

    a = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    b = secrets_module.get_secret(
        "anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY"
    )
    assert a == b == "sk-cached"
    # Only ONE SM hit despite two get_secret calls.
    assert fake_client.access_secret_version.call_count == 1
