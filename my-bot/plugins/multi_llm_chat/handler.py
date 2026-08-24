import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
from uuid import uuid4

from nonebot import get_bot, get_plugin_config, on_message, require
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.log import logger
from nonebot.matcher import current_event as nonebot_current_event
from nonebot.matcher import current_matcher as nonebot_current_matcher
from nonebot.params import EventMessage
from nonebot.rule import Rule

from .cli_runner import DockerCliRunner
from .config import Config
from .context import ContextBuilder
from .conversation import ConversationStore
from .daily_digest import DailyDigestService, load_daily_digest_config
from .identity import GroupRosterService
from .maintenance import ConversationMaintainer
from .media import ImageDownloadError, ImageStore
from .memory import GroupMemoryStore
from .models import ChatEvent, ConversationState, ImageResource
from .provider import LLMProvider
from .raw_request_store import cleanup_raw_request_logs
from .reply_tracker import (
    MatcherExecution,
    OutgoingReplyTracker,
    is_plugin_source,
)
from .tools import (
    AgentRunner,
    build_default_tool_registry,
    register_custom_tool_module,
)

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402  # isort:skip


PLUGIN_NAME = "multi_llm_chat"
SEND_GROUP_APIS = {"send_group_msg", "send_msg", "send_group_forward_msg"}
config = get_plugin_config(Config)
plugin_dir = Path(__file__).parent


def _resolve_plugin_path(configured: str, default: str) -> Path:
    path = Path(configured.strip() or default).expanduser()
    if not path.is_absolute():
        path = plugin_dir / path
    return path.resolve()


state_dir = _resolve_plugin_path(config.llm_chat_state_dir, "state")
identity_config_path = _resolve_plugin_path(
    config.llm_chat_identity_config_file,
    "identity_config.json",
)
daily_digest_config_path = _resolve_plugin_path(
    config.llm_chat_daily_digest_config_file,
    "daily_digest_config.json",
)
routes_path = _resolve_plugin_path(config.llm_chat_routes_file, "model_routes.json")
raw_request_log_dir = _resolve_plugin_path(
    config.llm_chat_raw_request_log_dir,
    "llm_request_logs",
)
cli_workspace_dir = _resolve_plugin_path(
    config.llm_chat_cli_workspace_dir,
    "tool_workspaces",
)

conversation_store = ConversationStore(
    state_dir=state_dir,
)
image_store = ImageStore(state_dir=state_dir)
roster_service = GroupRosterService(
    state_dir=state_dir,
    identity_config_path=identity_config_path,
)
memory_store = GroupMemoryStore(
    state_dir=state_dir,
    max_facts=config.llm_chat_memory_max_facts,
    max_aliases_per_member=config.llm_chat_memory_max_aliases_per_member,
)
provider = LLMProvider(
    config=config,
    routes_path=routes_path,
    raw_request_log_dir=raw_request_log_dir,
    logger=logger,
)
daily_digest_config = load_daily_digest_config(daily_digest_config_path)
daily_digest_service = DailyDigestService(
    provider=provider,
    config=daily_digest_config,
    logger=logger,
)
context_builder = ContextBuilder(
    roster_service=roster_service,
    memory_store=memory_store,
    image_store=image_store,
    char_budget=config.llm_chat_context_char_budget,
    recent_event_min_count=config.llm_chat_recent_event_min_count,
    max_events=config.llm_chat_conversation_max_events,
)
cli_runner = DockerCliRunner(
    image=config.llm_chat_cli_image,
    workspace_root=cli_workspace_dir,
    timeout_seconds=config.llm_chat_cli_timeout_seconds,
    output_max_chars=config.llm_chat_cli_output_max_chars,
)
tool_registry = build_default_tool_registry(
    roster_service=roster_service,
    memory_store=memory_store,
    cli_runner=cli_runner,
    tool_timeout_seconds=config.llm_chat_tool_timeout_seconds,
    output_max_chars=config.llm_chat_tool_output_max_chars,
)
register_custom_tool_module(tool_registry, config.llm_chat_custom_tools_module)
agent_runner = AgentRunner(
    provider=provider,
    registry=tool_registry,
    cli_runner=cli_runner,
    max_steps=config.llm_chat_max_tool_steps,
)
conversation_maintainer = ConversationMaintainer(
    provider=provider,
    context_builder=context_builder,
    conversation_store=conversation_store,
    memory_store=memory_store,
    image_store=image_store,
    roster_service=roster_service,
    summary_max_chars=config.llm_chat_summary_max_chars,
    logger=logger,
)
reply_tracker = OutgoingReplyTracker()

_group_locks: Dict[str, asyncio.Lock] = {}
_reply_locks: Dict[str, asyncio.Lock] = {}
_passive_tasks: Dict[str, asyncio.Task] = {}
_roster_sync_tasks: Dict[str, asyncio.Task] = {}
_maintenance_tasks: Dict[str, asyncio.Task] = {}


async def check_whitelist(event: GroupMessageEvent) -> bool:
    return str(event.group_id) in config.llm_chat_whitelist


def is_directed_at_bot(event: GroupMessageEvent) -> bool:
    if bool(getattr(event, "to_me", False)):
        return True
    original_message = getattr(event, "original_message", event.message)
    return any(
        segment.type == "at" and str(segment.data.get("qq", "")) == str(event.self_id)
        for segment in original_message
    )


llm_chat = on_message(
    rule=Rule(check_whitelist),
    priority=100,
    block=False,
)

llm_chat_mention = on_message(
    rule=Rule(check_whitelist) & Rule(is_directed_at_bot),
    priority=120,
    block=False,
)


@BaseBot.on_called_api
async def track_outgoing_group_message(
    bot: BaseBot,
    exception: Optional[Exception],
    api: str,
    data: Dict[str, Any],
    result: Any,
) -> None:
    if exception is not None or api not in SEND_GROUP_APIS:
        return
    matcher = nonebot_current_matcher.get(None)
    trigger_event = nonebot_current_event.get(None)
    if matcher is None or not isinstance(trigger_event, GroupMessageEvent):
        return
    source_plugin = str(
        getattr(matcher, "plugin_name", "")
        or getattr(matcher, "module_name", "")
        or matcher.__class__.__name__
    )
    if is_plugin_source(source_plugin, PLUGIN_NAME):
        return
    execution = MatcherExecution(
        event_id=_event_id(trigger_event),
        group_id=str(trigger_event.group_id),
        source_plugin=source_plugin,
    )
    group_id = str(data.get("group_id", ""))
    if not group_id or group_id != execution.group_id:
        return
    message_id = ""
    if isinstance(result, dict):
        message_id = str(result.get("message_id", "") or "")
    reply_tracker.record(execution, api=api, message_id=message_id)
    _cancel_passive_task(group_id)
    event = ChatEvent(
        event_id=f"outgoing:{group_id}:{message_id or uuid4().hex}",
        platform_message_id=message_id or None,
        source_event_id=execution.event_id,
        group_id=group_id,
        role="assistant",
        source=f"plugin:{execution.source_plugin}",
        content=_render_outgoing_message(data.get("message", "")),
        sent_at=datetime.now().astimezone(),
    )
    await conversation_store.append(event)


@llm_chat.handle()
async def handle_message(
    event: GroupMessageEvent,
    message: Message = EventMessage(),
) -> None:
    if is_directed_at_bot(event):
        return
    _cancel_passive_task(str(event.group_id))
    chat_event = await _ingest_event(event, message)
    if reply_tracker.has_external_reply(chat_event.event_id, PLUGIN_NAME):
        return
    _schedule_passive_reply(chat_event, event.self_id)


@llm_chat_mention.handle()
async def handle_mention_immediate(
    bot: Bot,
    event: GroupMessageEvent,
    message: Message = EventMessage(),
) -> None:
    _cancel_passive_task(str(event.group_id))
    chat_event = await _ingest_event(event, message)
    if config.llm_chat_reply_grace_seconds > 0:
        await asyncio.sleep(config.llm_chat_reply_grace_seconds)
    await _process_reply(
        bot=bot,
        trigger_event=chat_event,
        passive=False,
    )


async def _ingest_event(event: GroupMessageEvent, message: Message) -> ChatEvent:
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    event_id = _event_id(event)
    async with _group_lock(group_id):
        content, images = await ingest_message_content(message, event_id)
        reply_to_message_id = _reply_to_message_id(event)
        reply_to_event_id = None
        if reply_to_message_id is not None:
            current_state = await conversation_store.get(group_id)
            reply_to_event_id = next(
                (
                    item.event_id
                    for item in current_state.recent_events
                    if item.platform_message_id == reply_to_message_id
                ),
                None,
            )
        member = await roster_service.update_from_sender(
            group_id=group_id,
            user_id=user_id,
            sender=event.sender,
        )
        chat_event = ChatEvent(
            event_id=event_id,
            platform_message_id=str(event.message_id),
            group_id=group_id,
            role="user",
            source="onebot",
            user_id=user_id,
            display_name=roster_service.render_member_name(member, user_id),
            content=content,
            images=images,
            sent_at=_event_datetime(event),
            to_me=is_directed_at_bot(event),
            mentioned_user_ids=_mentioned_user_ids(event),
            reply_to_message_id=reply_to_message_id,
            reply_to_event_id=reply_to_event_id,
        )
        state = await conversation_store.append(chat_event)
    _ensure_roster_sync(event.self_id, group_id)
    _ensure_conversation_maintenance(group_id, state)
    return chat_event


def _schedule_passive_reply(event: ChatEvent, self_id: str) -> None:
    _cancel_passive_task(event.group_id)
    task = asyncio.create_task(_delayed_passive_reply(event, self_id))
    _passive_tasks[event.group_id] = task

    def remove_finished(finished: asyncio.Task) -> None:
        if _passive_tasks.get(event.group_id) is finished:
            del _passive_tasks[event.group_id]

    task.add_done_callback(remove_finished)


async def _delayed_passive_reply(event: ChatEvent, self_id: str) -> None:
    try:
        await asyncio.sleep(max(0, config.llm_chat_timeout))
        bot = _get_connected_bot(self_id)
        await _process_reply(bot=bot, trigger_event=event, passive=True)
    except asyncio.CancelledError:
        return
    except Exception as error:
        logger.opt(exception=error).error(
            "被动群聊处理失败: group_id={}, event_id={}, error_type={}",
            event.group_id,
            event.event_id,
            type(error).__name__,
        )


async def _process_reply(
    bot: Bot,
    trigger_event: ChatEvent,
    passive: bool,
) -> None:
    async with _reply_lock(trigger_event.group_id):
        await _run_reply(bot, trigger_event, passive)


async def _run_reply(
    bot: Bot,
    trigger_event: ChatEvent,
    passive: bool,
) -> None:
    if passive and not _passive_reply_is_current(trigger_event.group_id):
        return
    if reply_tracker.has_external_reply(trigger_event.event_id, PLUGIN_NAME):
        return
    state = await conversation_store.get(trigger_event.group_id)
    turn_mode = "passive" if passive else "direct"
    reply_targets = _reply_targets(state)
    roster = await roster_service.get_roster(trigger_event.group_id)
    bot_user_id = str(getattr(bot, "self_id", "") or "")
    mentionable_user_ids = sorted(
        user_id for user_id in roster.members if user_id != bot_user_id
    )
    allow_finish_without_reply = passive or _has_llm_reply_after_trigger(
        state,
        trigger_event,
    )
    agent_messages = await context_builder.build(
        trigger_event.group_id,
        state,
        turn_mode=turn_mode,
        trigger_event=trigger_event,
        allow_finish_without_reply=allow_finish_without_reply,
        include_images=provider.image_understanding_enabled(),
    )
    result = await agent_runner.run(
        messages=agent_messages,
        turn_id=uuid4().hex,
        group_id=trigger_event.group_id,
        triggering_user_id=trigger_event.user_id or "",
        bot=bot,
        turn_mode=turn_mode,
        allow_finish_without_reply=allow_finish_without_reply,
        replyable_event_ids=sorted(reply_targets),
        mentionable_user_ids=mentionable_user_ids,
    )
    if result.action == "skip":
        logger.debug(
            "大模型决定不参与群聊: group_id={}, event_id={}, reason={}",
            trigger_event.group_id,
            trigger_event.event_id,
            result.skip_reason,
        )
        return
    if passive and not _passive_reply_is_current(trigger_event.group_id):
        return
    if reply_tracker.has_external_reply(trigger_event.event_id, PLUGIN_NAME):
        logger.info(
            "其他插件已回复，取消大模型发送: group_id={}, event_id={}",
            trigger_event.group_id,
            trigger_event.event_id,
        )
        return
    outgoing_message = _build_outgoing_message(
        content=result.content,
        reply_to_event_id=result.reply_to_event_id,
        mention_user_ids=result.mention_user_ids,
        reply_targets=reply_targets,
        mentionable_user_ids=set(mentionable_user_ids),
    )
    send_result = await bot.call_api(
        "send_group_msg",
        group_id=int(trigger_event.group_id),
        message=outgoing_message,
    )
    message_id = ""
    if isinstance(send_result, dict):
        message_id = str(send_result.get("message_id", "") or "")
    await conversation_store.append(
        ChatEvent(
            event_id=f"llm:{trigger_event.group_id}:{message_id or uuid4().hex}",
            platform_message_id=message_id or None,
            source_event_id=trigger_event.event_id,
            group_id=trigger_event.group_id,
            role="assistant",
            source="llm",
            content=result.content,
            sent_at=datetime.now().astimezone(),
            mentioned_user_ids=list(result.mention_user_ids),
            reply_to_message_id=(
                reply_targets.get(result.reply_to_event_id)
                if result.reply_to_event_id
                else None
            ),
            reply_to_event_id=result.reply_to_event_id,
        )
    )


def _reply_targets(state: ConversationState) -> Dict[str, str]:
    targets: Dict[str, str] = {}
    for event in state.recent_events:
        message_id = str(event.platform_message_id or "").strip()
        if not message_id:
            continue
        try:
            normalized_message_id = str(int(message_id))
        except ValueError:
            continue
        targets[event.event_id] = normalized_message_id
    return targets


def _build_outgoing_message(
    content: str,
    reply_to_event_id: Optional[str],
    mention_user_ids: Tuple[str, ...],
    reply_targets: Dict[str, str],
    mentionable_user_ids: set[str],
) -> Any:
    if reply_to_event_id is None and not mention_user_ids:
        return content
    segments: List[MessageSegment] = []
    if reply_to_event_id is not None:
        reply_message_id = reply_targets.get(reply_to_event_id)
        if reply_message_id is None:
            raise RuntimeError("引用目标不属于当前可回复事件")
        segments.append(MessageSegment.reply(int(reply_message_id)))
    for user_id in mention_user_ids:
        if user_id not in mentionable_user_ids:
            raise RuntimeError("@目标不是当前群成员")
        segments.append(MessageSegment.at(user_id))
        segments.append(MessageSegment.text(" "))
    segments.append(MessageSegment.text(content))
    return Message(segments)


def _has_llm_reply_after_trigger(
    state: ConversationState,
    trigger_event: ChatEvent,
) -> bool:
    trigger_index = next(
        (
            index
            for index, event in enumerate(state.recent_events)
            if event.event_id == trigger_event.event_id
        ),
        None,
    )
    if trigger_index is not None:
        later_events = state.recent_events[trigger_index + 1 :]
    else:
        later_events = [
            event
            for event in state.recent_events
            if event.sent_at > trigger_event.sent_at
        ]
    return any(
        event.role == "assistant" and event.source == "llm" for event in later_events
    )


def _ensure_roster_sync(self_id: str, group_id: str) -> None:
    current = _roster_sync_tasks.get(group_id)
    if current is not None and not current.done():
        return

    async def sync() -> None:
        try:
            bot = _get_connected_bot(self_id)
            roster = await roster_service.get_roster(group_id)
            age = (datetime.now().astimezone() - roster.synced_at).total_seconds()
            if (
                len(roster.members) <= 1
                or age >= config.llm_chat_roster_refresh_seconds
            ):
                await roster_service.sync_group(bot, group_id)
        except Exception as error:
            logger.opt(exception=error).warning(
                "同步 OneBot 群成员失败: group_id={}, error_type={}",
                group_id,
                type(error).__name__,
            )

    task = asyncio.create_task(sync())
    _roster_sync_tasks[group_id] = task


def _ensure_conversation_maintenance(
    group_id: str,
    state: ConversationState,
) -> None:
    compressible, _ = context_builder.split_for_compression(state.recent_events)
    if not compressible:
        return
    current = _maintenance_tasks.get(group_id)
    if current is not None and not current.done():
        return

    async def maintain() -> None:
        try:
            latest_state = await conversation_store.get(group_id)
            await conversation_maintainer.maintain_if_needed(
                group_id,
                latest_state,
            )
        except Exception as error:
            logger.error(
                "后台对话压缩失败: group_id={}, error_type={}",
                group_id,
                type(error).__name__,
            )

    task = asyncio.create_task(maintain())
    _maintenance_tasks[group_id] = task


def _cancel_passive_task(group_id: str) -> None:
    task = _passive_tasks.pop(group_id, None)
    if task is not None and not task.done():
        task.cancel()


def _passive_reply_is_current(group_id: str) -> bool:
    current_task = asyncio.current_task()
    return current_task is not None and _passive_tasks.get(group_id) is current_task


def _group_lock(group_id: str) -> asyncio.Lock:
    return _group_locks.setdefault(group_id, asyncio.Lock())


def _reply_lock(group_id: str) -> asyncio.Lock:
    return _reply_locks.setdefault(group_id, asyncio.Lock())


def _get_connected_bot(self_id: Any) -> Bot:
    return cast(Bot, get_bot(str(self_id)))


def _event_id(event: GroupMessageEvent) -> str:
    message_id = str(getattr(event, "message_id", "") or "")
    if message_id:
        return f"onebot:{event.group_id}:{message_id}"
    return f"onebot:{event.group_id}:{event.user_id}:{event.time}"


def _event_datetime(event: GroupMessageEvent) -> datetime:
    timestamp = int(getattr(event, "time", 0) or 0)
    if timestamp > 0:
        return datetime.fromtimestamp(timestamp).astimezone()
    return datetime.now().astimezone()


def _mentioned_user_ids(event: GroupMessageEvent) -> List[str]:
    original_message = getattr(event, "original_message", event.message)
    return sorted(
        {
            str(segment.data.get("qq", ""))
            for segment in original_message
            if segment.type == "at"
            and str(segment.data.get("qq", ""))
            and str(segment.data.get("qq", "")) != "all"
            and str(segment.data.get("qq", "")) != str(event.self_id)
        }
    )


def _reply_to_message_id(event: GroupMessageEvent) -> Optional[str]:
    original_message = getattr(event, "original_message", event.message)
    return next(
        (
            str(segment.data.get("id", ""))
            for segment in original_message
            if segment.type == "reply" and str(segment.data.get("id", ""))
        ),
        None,
    )


def render_message_content(message: Message) -> str:
    return "".join(_render_segment(segment) for segment in message).strip()


async def ingest_message_content(
    message: Message,
    media_namespace: str,
) -> Tuple[str, List[ImageResource]]:
    parts: List[str] = []
    images: List[ImageResource] = []
    raw_length = 0
    for segment in message:
        rendered = _render_segment(segment)
        if segment.type == "image":
            url = str(segment.data.get("url", "") or "").strip()
            if url:
                try:
                    images.append(
                        await image_store.download(
                            url,
                            placeholder=rendered,
                            content_offset=raw_length,
                            media_namespace=media_namespace,
                        )
                    )
                except ImageDownloadError as error:
                    logger.warning(
                        "接收图片失败: file={}, error_type={}",
                        str(segment.data.get("file", "") or ""),
                        type(error).__name__,
                    )
            else:
                logger.warning(
                    "接收图片缺少下载地址: file={}",
                    str(segment.data.get("file", "") or ""),
                )
        parts.append(rendered)
        raw_length += len(rendered)
    raw_content = "".join(parts)
    leading_whitespace = len(raw_content) - len(raw_content.lstrip())
    content = raw_content.strip()
    adjusted_images = [
        image.model_copy(
            update={"content_offset": image.content_offset - leading_whitespace}
        )
        for image in images
    ]
    return content, adjusted_images


def _render_segment(segment: Any) -> str:
    if segment.type == "text":
        return str(segment.data.get("text", ""))
    if segment.type == "at":
        target = str(segment.data.get("qq", ""))
        return "@全体成员" if target == "all" else f"@用户{target}"
    if segment.type == "image":
        summary = str(segment.data.get("summary", "") or "")
        return f"[图片{f':{summary}' if summary else ''}]"
    if segment.type == "face":
        return f"[表情:{segment.data.get('id', '')}]"
    if segment.type == "record":
        return "[语音]"
    if segment.type == "video":
        return "[视频]"
    if segment.type == "file":
        return "[文件]"
    if segment.type == "node":
        return "[转发消息]"
    if segment.type == "json":
        return "[卡片消息]"
    return f"[{segment.type}]"


def _render_outgoing_message(message: Any) -> str:
    if isinstance(message, Message):
        return render_message_content(message)
    return str(message).strip()


@scheduler.scheduled_job(
    "interval",
    seconds=max(60, config.llm_chat_roster_refresh_seconds),
)
async def refresh_group_rosters() -> None:
    try:
        bot = get_bot()
    except ValueError:
        return
    for group_id in config.llm_chat_whitelist:
        try:
            await roster_service.sync_group(bot, group_id)
        except Exception as error:
            logger.warning(
                "定时同步 OneBot 群成员失败: group_id={}, error_type={}",
                group_id,
                type(error).__name__,
            )


@scheduler.scheduled_job("cron", hour=0, minute=10)
async def clean_expired_raw_request_logs() -> None:
    try:
        deleted_count = await asyncio.to_thread(
            cleanup_raw_request_logs,
            raw_request_log_dir,
            config.llm_chat_raw_request_retention_days,
        )
    except OSError as error:
        logger.error(
            "清理大模型原始请求日志失败: error_type={}",
            type(error).__name__,
        )
        return
    if deleted_count:
        logger.info("已清理过期大模型原始请求日志: file_count={}", deleted_count)


@scheduler.scheduled_job(
    "cron",
    hour=daily_digest_config.send_time.hour,
    minute=daily_digest_config.send_time.minute,
    timezone=daily_digest_config.timezone,
    max_instances=1,
)
async def send_daily_digests() -> None:
    try:
        bot = cast(Bot, get_bot())
    except ValueError:
        logger.error("日报发送失败: 当前没有已连接的 OneBot 实例")
        return
    for group in daily_digest_config.groups:
        if not group.enabled:
            continue
        _cancel_passive_task(group.group_id)
        try:
            async with _reply_lock(group.group_id):
                message = await daily_digest_service.build_message(group)
                send_result = await bot.call_api(
                    "send_group_msg",
                    group_id=int(group.group_id),
                    message=message,
                )
                message_id = ""
                if isinstance(send_result, dict):
                    message_id = str(send_result.get("message_id", "") or "")
                sent_at = datetime.now().astimezone()
                state = await conversation_store.append(
                    ChatEvent(
                        event_id=(
                            f"llm:daily_digest:{group.group_id}:"
                            f"{message_id or uuid4().hex}"
                        ),
                        platform_message_id=message_id or None,
                        group_id=group.group_id,
                        role="assistant",
                        source="llm:daily_digest",
                        content=message,
                        sent_at=sent_at,
                    )
                )
                _ensure_conversation_maintenance(group.group_id, state)
                logger.info(
                    "日报发送成功: group_id={}, message_id={}, chars={}",
                    group.group_id,
                    message_id,
                    len(message),
                )
        except Exception as error:
            logger.opt(exception=error).error(
                "日报生成或发送失败: group_id={}, error_type={}",
                group.group_id,
                type(error).__name__,
            )
