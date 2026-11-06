"""Server-validated demo identity and role mapping.

The local reference uses bearer tokens loaded from the server environment or a
data-directory identity file. Browser-supplied actor/role fields are ignored.
This is a documented demo mechanism, not enterprise federation.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

__all__ = [
    "DemoIdentity",
    "IdentityProvider",
    "Principal",
    "get_principal",
]


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    roles: tuple[str, ...]
    project_ids: tuple[str, ...] = ("*",)

    def can_access(self, project_id: str) -> bool:
        return "*" in self.project_ids or project_id in self.project_ids

    def is_manager(self) -> bool:
        return "manager" in self.roles

    def is_planner(self) -> bool:
        return "planner" in self.roles


@dataclass
class DemoIdentity:
    token: str
    principal: Principal

    def public(self) -> dict[str, Any]:
        return {
            "actor_id": self.principal.actor_id,
            "roles": list(self.principal.roles),
            "project_ids": list(self.principal.project_ids),
            "token_hint": f"stored server-side ({len(self.token)} chars)",
        }


@dataclass
class IdentityProvider:
    identities: list[DemoIdentity] = field(default_factory=list)

    def add(self, token: str, principal: Principal) -> None:
        self.identities.append(DemoIdentity(token=token, principal=principal))

    def resolve(self, token: str) -> Principal | None:
        for identity in self.identities:
            if secrets.compare_digest(identity.token, token):
                return identity.principal
        return None

    def public_identities(self) -> list[dict[str, Any]]:
        return [identity.public() for identity in self.identities]


def load_or_create_demo_identities(data_dir: Path) -> IdentityProvider:
    """Load stable local demo identities, creating random tokens once.

    Tokens are server-side credentials. They are written to a data-directory
    file and never included in evidence or the frontend bundle.
    """

    path = data_dir / "demo-identities.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        provider = IdentityProvider()
        for item in payload["identities"]:
            provider.add(
                item["token"],
                Principal(
                    actor_id=item["actor_id"],
                    roles=tuple(item["roles"]),
                    project_ids=tuple(item.get("project_ids", ["*"])),
                ),
            )
        return provider

    data_dir.mkdir(parents=True, exist_ok=True)
    provider = IdentityProvider()
    planner_token = secrets.token_urlsafe(24)
    manager_token = secrets.token_urlsafe(24)
    provider.add(
        planner_token,
        Principal(actor_id="planner.demo", roles=("planner",)),
    )
    provider.add(
        manager_token,
        Principal(actor_id="manager.demo", roles=("manager",)),
    )
    path.write_text(
        json.dumps(
            {
                "note": "Local synthetic demo identities. Keep this file private.",
                "identities": [
                    {
                        "token": planner_token,
                        "actor_id": "planner.demo",
                        "roles": ["planner"],
                        "project_ids": ["*"],
                    },
                    {
                        "token": manager_token,
                        "actor_id": "manager.demo",
                        "roles": ["manager"],
                        "project_ids": ["*"],
                    },
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return provider


def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    provider: IdentityProvider | None = getattr(request.app.state, "identity", None)
    if provider is None:
        raise HTTPException(status_code=503, detail="Identity is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Bearer token required."},
        )
    principal = provider.resolve(authorization.removeprefix("Bearer ").strip())
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Unknown bearer token."},
        )
    return principal


def require_manager(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_manager():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "Only an authorised manager can decide.",
            },
        )
    return principal
