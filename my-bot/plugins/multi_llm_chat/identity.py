import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .json_store import atomic_write_json_model, load_json_model
from .models import (
    GroupIdentityConfig,
    GroupRoster,
    IdentityConfig,
    RosterMember,
)


class GroupRosterService:
    def __init__(self, state_dir: Path, identity_config_path: Path) -> None:
        self._directory = state_dir / "group_rosters"
        self._identity_config_path = identity_config_path
        self._locks: Dict[str, asyncio.Lock] = {}

    def _path(self, group_id: str) -> Path:
        return self._directory / f"{group_id}.json"

    def _lock(self, group_id: str) -> asyncio.Lock:
        return self._locks.setdefault(group_id, asyncio.Lock())

    def load_identity_config(self) -> IdentityConfig:
        return load_json_model(
            self._identity_config_path,
            IdentityConfig,
            IdentityConfig,
        )

    def group_identity_config(self, group_id: str) -> GroupIdentityConfig:
        config = self.load_identity_config()
        return config.groups.get(group_id, GroupIdentityConfig())

    def _load_roster(self, group_id: str) -> GroupRoster:
        return load_json_model(
            self._path(group_id),
            GroupRoster,
            lambda: GroupRoster(
                group_id=group_id, synced_at=datetime.now().astimezone()
            ),
        )

    async def get_roster(self, group_id: str) -> GroupRoster:
        async with self._lock(group_id):
            return await asyncio.to_thread(self._load_roster, group_id)

    async def sync_group(self, bot: Any, group_id: str) -> GroupRoster:
        group_number = int(group_id)
        group_info, members_data = await asyncio.gather(
            bot.call_api("get_group_info", group_id=group_number, no_cache=True),
            bot.call_api("get_group_member_list", group_id=group_number),
        )
        members: Dict[str, RosterMember] = {}
        for raw_member in members_data or []:
            member = _parse_roster_member(raw_member)
            members[member.user_id] = member
        roster = GroupRoster(
            group_id=group_id,
            group_name=str((group_info or {}).get("group_name", "")),
            synced_at=datetime.now().astimezone(),
            members=members,
        )
        async with self._lock(group_id):
            await asyncio.to_thread(
                atomic_write_json_model,
                self._path(group_id),
                roster,
            )
        return roster

    async def get_member(
        self,
        bot: Any,
        group_id: str,
        user_id: str,
        refresh: bool = False,
    ) -> Optional[RosterMember]:
        if not refresh:
            roster = await self.get_roster(group_id)
            if user_id in roster.members:
                return roster.members[user_id]
        raw_member = await bot.call_api(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=True,
        )
        if not raw_member:
            return None
        member = _parse_roster_member(raw_member)
        async with self._lock(group_id):
            roster = await asyncio.to_thread(self._load_roster, group_id)
            roster.members[user_id] = member
            roster.synced_at = datetime.now().astimezone()
            await asyncio.to_thread(
                atomic_write_json_model,
                self._path(group_id),
                roster,
            )
        return member

    async def update_from_sender(
        self,
        group_id: str,
        user_id: str,
        sender: Any,
    ) -> RosterMember:
        raw_sender = sender if isinstance(sender, dict) else _object_to_dict(sender)
        async with self._lock(group_id):
            roster = await asyncio.to_thread(self._load_roster, group_id)
            existing = roster.members.get(user_id)
            nickname = (
                str(raw_sender.get("nickname", ""))
                if "nickname" in raw_sender
                else (existing.nickname if existing else "")
            )
            card = (
                str(raw_sender.get("card", ""))
                if "card" in raw_sender
                else (existing.card if existing else "")
            )
            member = RosterMember(
                user_id=user_id,
                nickname=nickname,
                card=card,
                role=str(
                    raw_sender.get("role", "")
                    or (existing.role if existing else "member")
                ),
                join_time=existing.join_time if existing else 0,
                last_sent_time=int(datetime.now().timestamp()),
                title=str(
                    raw_sender.get("title", "") or (existing.title if existing else "")
                ),
            )
            roster.members[user_id] = member
            await asyncio.to_thread(
                atomic_write_json_model,
                self._path(group_id),
                roster,
            )
        return member

    def render_member_name(self, member: Optional[RosterMember], user_id: str) -> str:
        if member is None:
            return f"用户{user_id}"
        return member.card or member.nickname or f"用户{user_id}"

    def pinned_aliases(self, user_id: str) -> List[str]:
        member_config = self.load_identity_config().members.get(user_id)
        return list(member_config.pinned_aliases) if member_config else []


def _parse_roster_member(raw: Dict[str, Any]) -> RosterMember:
    return RosterMember(
        user_id=str(raw.get("user_id", "")),
        nickname=str(raw.get("nickname", "") or ""),
        card=str(raw.get("card", "") or ""),
        role=str(raw.get("role", "member") or "member"),
        join_time=int(raw.get("join_time", 0) or 0),
        last_sent_time=int(raw.get("last_sent_time", 0) or 0),
        title=str(raw.get("title", "") or ""),
    )


def _object_to_dict(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "dict"):
        return dict(value.dict())
    result: Dict[str, Any] = {}
    for field in ("nickname", "card", "role", "title"):
        if hasattr(value, field):
            result[field] = getattr(value, field)
    return result
