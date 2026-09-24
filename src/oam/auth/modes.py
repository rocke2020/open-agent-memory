"""Request identity asserted by a local process or trusted upstream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AssertedIdentity:
    mode: Literal["dev", "trusted"]
    tenant_id: str
    user_id: str
    root_secret: str | None = None
    role: Literal["user", "admin"] = "user"


RequestActor = str | AssertedIdentity
