"""AC 14: ManagerPermission predicates."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from utils.managerPermission import ManagerPermission


def predicate_of(decorator: Any) -> Any:
    """Apply the check decorator to a dummy function and pull out the predicate."""

    async def f(*_a: Any) -> None:
        return None

    out = decorator(f)
    checks = getattr(out, "__discord_app_commands_checks__", None) or getattr(
        out, "__commands_checks__", None
    )
    assert checks, "no check attached"
    return checks[-1]


def member(top: int) -> Any:
    m = MagicMock(spec=discord.Member)
    m.top_role = top
    return m


def interaction(guild: Any, user: Any) -> Any:
    return SimpleNamespace(guild=guild, user=user)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_isrole_no_guild() -> None:
    p = predicate_of(ManagerPermission.isRole(5))
    assert run(p(interaction(None, member(1)))) is False


def test_isrole_role_missing() -> None:
    p = predicate_of(ManagerPermission.isRole(5))
    guild = SimpleNamespace(get_role=lambda _id: None)
    assert run(p(interaction(guild, member(1)))) is False


def test_isrole_user_not_member() -> None:
    p = predicate_of(ManagerPermission.isRole(5))
    guild = SimpleNamespace(get_role=lambda _id: 3)
    user = SimpleNamespace(id=1)
    assert run(p(interaction(guild, user))) is False


@pytest.mark.parametrize(("top", "role", "expected"), [(5, 3, True), (3, 3, True), (2, 3, False)])
def test_isrole_comparison(top: int, role: int, expected: bool) -> None:
    p = predicate_of(ManagerPermission.isRole(99))
    seen: list[int] = []

    def get_role(i: int) -> int:
        seen.append(i)
        return role

    guild = SimpleNamespace(get_role=get_role)
    assert run(p(interaction(guild, member(top)))) is expected
    assert seen == [99]


def test_isrole_real_role_objects() -> None:
    """discord.Role supports >=; make sure a real comparison path works."""
    p = predicate_of(ManagerPermission.isRole(1))
    hi, lo = MagicMock(spec=discord.Role), MagicMock(spec=discord.Role)
    hi.__ge__ = lambda s, o: True  # type: ignore[method-assign]
    guild = SimpleNamespace(get_role=lambda _i: lo)
    m = MagicMock(spec=discord.Member)
    m.top_role = hi
    assert run(p(interaction(guild, m))) is True


def test_isuser() -> None:
    p = predicate_of(ManagerPermission.isUser(42))
    assert run(p(SimpleNamespace(user=SimpleNamespace(id=42)))) is True
    assert run(p(SimpleNamespace(user=SimpleNamespace(id=43)))) is False


@pytest.mark.parametrize("owner", [True, False])
def test_isowner_app(owner: bool) -> None:
    p = predicate_of(ManagerPermission.isOwnerApp())
    client = SimpleNamespace(is_owner=AsyncMock(return_value=owner))
    i = SimpleNamespace(client=client, user="U")
    assert run(p(i)) is owner
    client.is_owner.assert_awaited_once_with("U")


@pytest.mark.parametrize("owner", [True, False])
def test_isowner_prefix(owner: bool) -> None:
    p = predicate_of(ManagerPermission.isOwner())
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=owner))
    ctx = SimpleNamespace(bot=bot, author="A")
    assert run(p(ctx)) is owner
    bot.is_owner.assert_awaited_once_with("A")


def test_decorators_return_types() -> None:
    assert callable(ManagerPermission.isOwner())
    assert callable(ManagerPermission.isOwnerApp())
    assert app_commands is not None and commands is not None
