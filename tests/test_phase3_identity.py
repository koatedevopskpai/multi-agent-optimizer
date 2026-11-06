"""Phase 3B server-derived identity and role tests."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from delivery_recovery.api.app import create_app
from delivery_recovery.api.identity import (
    IdentityProvider,
    Principal,
    get_principal,
    load_or_create_demo_identities,
    require_manager,
)
from delivery_recovery.api.runtime import build_runtime
from delivery_recovery.storage.config import StorageConfig


def test_principal_access_and_roles():
    manager = Principal(actor_id="m", roles=("manager",), project_ids=("P1",))
    assert manager.can_access("P1") is True
    assert manager.can_access("P2") is False
    assert manager.is_manager() is True
    assert manager.is_planner() is False
    wildcard = Principal(actor_id="p", roles=("planner",))
    assert wildcard.can_access("anything") is True
    assert wildcard.is_planner() is True


def test_identity_file_is_stable_and_tokens_resolve(tmp_path):
    first = load_or_create_demo_identities(tmp_path)
    second = load_or_create_demo_identities(tmp_path)
    first_tokens = {item.principal.actor_id: item.token for item in first.identities}
    second_tokens = {item.principal.actor_id: item.token for item in second.identities}
    assert first_tokens == second_tokens
    assert (tmp_path / "demo-identities.json").exists()
    payload = json.loads((tmp_path / "demo-identities.json").read_text(encoding="utf-8"))
    assert len(payload["identities"]) == 2
    assert first.resolve(first_tokens["manager.demo"]).is_manager()
    assert first.resolve("not-a-token") is None


def test_require_manager_rejects_planner():
    with pytest.raises(HTTPException) as error:
        require_manager(Principal(actor_id="p", roles=("planner",)))
    assert error.value.status_code == 403


def test_unknown_token_is_401(tmp_path):
    config = StorageConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "app.db",
        checkpoint_path=tmp_path / "cp.db",
    )
    runtime = build_runtime(config, start_worker=False)
    try:
        client = TestClient(create_app(runtime))
        response = client.get(
            "/v1/runs/missing", headers={"Authorization": "Bearer unknown"}
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "UNAUTHENTICATED"
    finally:
        runtime.close()


def test_identity_provider_without_header_is_401(tmp_path):
    provider = IdentityProvider()
    provider.add("token", Principal(actor_id="p", roles=("planner",)))
    assert provider.public_identities()[0]["actor_id"] == "p"
    with pytest.raises(HTTPException):
        # Simulate the dependency with no Authorization header.
        from delivery_recovery.api import identity as identity_module

        class Request:
            app = type("App", (), {"state": type("State", (), {"identity": provider})()})()

        identity_module.get_principal(Request(), authorization=None)  # type: ignore[arg-type]


def test_require_manager_success_and_missing_identity_configuration():
    manager = Principal(actor_id="m", roles=("manager",))
    assert require_manager(manager) is manager

    from delivery_recovery.api import identity as identity_module

    class Request:
        app = type("App", (), {"state": type("State", (), {})()})()

    with pytest.raises(HTTPException) as error:
        identity_module.get_principal(Request(), authorization="Bearer token")  # type: ignore[arg-type]
    assert error.value.status_code == 503
