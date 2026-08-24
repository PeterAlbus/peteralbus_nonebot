import asyncio
import copy
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .cli_runner import DockerCliRunner
from .identity import GroupRosterService
from .memory import GroupMemoryStore, normalize_memory_text
from .provider import LLMProvider


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArguments(ToolArguments):
    pass


class MemberArguments(ToolArguments):
    user_id: str


class MemorySearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=100)


class CliArguments(ToolArguments):
    command: str = Field(min_length=1, max_length=4000)
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=120)


SkipReplyReason = Literal[
    "addressed_to_others",
    "already_answered",
    "repeated_content",
    "low_incremental_value",
    "insufficient_context",
    "bot_spoke_recently",
]


class FinishWithoutReplyArguments(ToolArguments):
    reason: SkipReplyReason


class ReplyToEventArguments(ToolArguments):
    event_id: str = Field(min_length=1, max_length=200)


class MentionMembersArguments(ToolArguments):
    user_ids: List[str] = Field(min_length=1, max_length=8)


FINISH_WITHOUT_REPLY_TOOL_NAME = "finish_without_reply"
REPLY_TO_EVENT_TOOL_NAME = "reply_to_event"
MENTION_MEMBERS_TOOL_NAME = "mention_members"


@dataclass(frozen=True)
class ToolContext:
    turn_id: str
    group_id: str
    triggering_user_id: str
    bot: Any
    workspace: Path


ToolExecutor = Callable[[ToolContext, BaseModel], Awaitable[Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments_model: Type[BaseModel]
    executor: ToolExecutor
    timeout_seconds: int

    def api_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": strict_model_json_schema(self.arguments_model),
                "strict": True,
            },
        }


class ToolRegistry:
    def __init__(self, output_max_chars: int) -> None:
        self._definitions: Dict[str, ToolDefinition] = {}
        self._output_max_chars = max(1000, output_max_chars)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"工具名称重复: {definition.name}")
        schema = definition.arguments_model.model_json_schema()
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"工具参数模型必须设置 extra='forbid': {definition.name}")
        self._definitions[definition.name] = definition

    def schemas(self) -> List[Dict[str, Any]]:
        return [definition.api_schema() for definition in self._definitions.values()]

    async def execute(
        self,
        name: str,
        raw_arguments: str,
        context: ToolContext,
    ) -> str:
        definition = self._definitions.get(name)
        if definition is None:
            return _json_result(False, error=f"未注册工具: {name}")
        try:
            arguments = definition.arguments_model.model_validate_json(raw_arguments)
        except ValidationError as error:
            return _json_result(False, error=f"工具参数校验失败: {error}")
        try:
            result = await asyncio.wait_for(
                definition.executor(context, arguments),
                timeout=definition.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return _json_result(False, error="工具执行超时")
        except Exception as error:
            return _json_result(False, error=f"工具执行失败: {type(error).__name__}")
        payload = _json_result(True, data=result)
        if len(payload) <= self._output_max_chars:
            return payload
        return _json_result(
            True,
            data={
                "output": payload[: self._output_max_chars],
                "truncated": True,
            },
        )


def register_custom_tool_module(registry: ToolRegistry, module_name: str) -> None:
    normalized = module_name.strip()
    if not normalized:
        return
    module = importlib.import_module(normalized)
    register_tools = getattr(module, "register_tools", None)
    if not callable(register_tools):
        raise TypeError(f"自定义工具模块缺少 register_tools(registry): {normalized}")
    register_tools(registry)


def strict_model_json_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    schema = copy.deepcopy(model.model_json_schema())
    definitions = schema.pop("$defs", {})
    schema = _inline_json_schema_references(schema, definitions)
    _make_json_schema_strict(schema)
    return schema


def _inline_json_schema_references(
    value: Any,
    definitions: Dict[str, Any],
) -> Any:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            definition_name = reference.removeprefix("#/$defs/")
            if definition_name not in definitions:
                raise ValueError(f"JSON Schema 引用了未知定义: {definition_name}")
            resolved = copy.deepcopy(definitions[definition_name])
            resolved.update({key: item for key, item in value.items() if key != "$ref"})
            return _inline_json_schema_references(resolved, definitions)
        return {
            key: _inline_json_schema_references(item, definitions)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_inline_json_schema_references(item, definitions) for item in value]
    return value


def _make_json_schema_strict(value: Any) -> None:
    if isinstance(value, dict):
        for unsupported_keyword in (
            "default",
            "title",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minItems",
            "maxItems",
        ):
            value.pop(unsupported_keyword, None)
        if value.get("type") == "object" or "properties" in value:
            properties = value.get("properties", {})
            value["additionalProperties"] = False
            value["required"] = list(properties)
        for nested in value.values():
            _make_json_schema_strict(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _make_json_schema_strict(nested)


@dataclass(frozen=True)
class AgentRunResult:
    action: Literal["reply", "skip"]
    content: str
    tool_steps: int
    skip_reason: str = ""
    reply_to_event_id: Optional[str] = None
    mention_user_ids: Tuple[str, ...] = ()


@dataclass
class ReplyDraft:
    reply_to_event_id: Optional[str] = None
    mention_user_ids: List[str] = field(default_factory=list)


class AgentRunner:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        cli_runner: DockerCliRunner,
        max_steps: int,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._cli_runner = cli_runner
        self._max_steps = max(1, max_steps)

    async def run(
        self,
        messages: List[Dict[str, Any]],
        turn_id: str,
        group_id: str,
        triggering_user_id: str,
        bot: Any,
        turn_mode: Literal["direct", "passive"],
        allow_finish_without_reply: bool,
        replyable_event_ids: Sequence[str],
        mentionable_user_ids: Sequence[str],
    ) -> AgentRunResult:
        workspace = self._cli_runner.create_workspace(turn_id)
        context = ToolContext(
            turn_id=turn_id,
            group_id=group_id,
            triggering_user_id=triggering_user_id,
            bot=bot,
            workspace=workspace,
        )
        conversation = list(messages)
        replyable_events = set(replyable_event_ids)
        mentionable_users = set(mentionable_user_ids)
        draft = ReplyDraft()
        tools = self._registry.schemas()
        if replyable_events:
            tools.append(reply_to_event_schema())
        if mentionable_users:
            tools.append(mention_members_schema())
        if allow_finish_without_reply:
            tools.append(finish_without_reply_schema())
        try:
            for step in range(self._max_steps + 1):
                turn = await self._provider.complete(
                    messages=conversation,
                    request_type=f"{turn_mode}_chat_agent",
                    turn_id=turn_id,
                    step=step,
                    tools=tools,
                    tool_choice="auto",
                    allow_builtin_tools=True,
                )
                if not turn.tool_calls:
                    if not turn.content:
                        raise RuntimeError("大模型没有产生回复或沉默动作")
                    return AgentRunResult(
                        action="reply",
                        content=turn.content,
                        tool_steps=step,
                        reply_to_event_id=draft.reply_to_event_id,
                        mention_user_ids=tuple(draft.mention_user_ids),
                    )
                skip_calls = [
                    call
                    for call in turn.tool_calls
                    if call.function.name == FINISH_WITHOUT_REPLY_TOOL_NAME
                ]
                if skip_calls:
                    if not allow_finish_without_reply:
                        raise RuntimeError("当前轮次不允许无回复终止")
                    if len(turn.tool_calls) != 1 or turn.content:
                        raise RuntimeError("沉默动作不能与正文或其他工具同时使用")
                    arguments = FinishWithoutReplyArguments.model_validate_json(
                        skip_calls[0].function.arguments
                    )
                    return AgentRunResult(
                        action="skip",
                        content="",
                        tool_steps=step,
                        skip_reason=arguments.reason,
                    )
                if step >= self._max_steps:
                    raise RuntimeError("大模型工具调用超过最大轮次")
                conversation.append(turn.as_message())
                for tool_call in turn.tool_calls:
                    if tool_call.function.name == REPLY_TO_EVENT_TOOL_NAME:
                        result = apply_reply_to_event(
                            draft,
                            tool_call.function.arguments,
                            replyable_events,
                        )
                    elif tool_call.function.name == MENTION_MEMBERS_TOOL_NAME:
                        result = apply_mentions(
                            draft,
                            tool_call.function.arguments,
                            mentionable_users,
                        )
                    else:
                        result = await self._registry.execute(
                            tool_call.function.name,
                            tool_call.function.arguments,
                            context,
                        )
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        }
                    )
            raise RuntimeError("大模型工具调用没有产生最终回复")
        finally:
            await asyncio.to_thread(self._cli_runner.remove_workspace, workspace)


def apply_reply_to_event(
    draft: ReplyDraft,
    raw_arguments: str,
    replyable_event_ids: set[str],
) -> str:
    try:
        arguments = ReplyToEventArguments.model_validate_json(raw_arguments)
    except ValidationError as error:
        return _json_result(False, error=f"工具参数校验失败: {error}")
    if arguments.event_id not in replyable_event_ids:
        return _json_result(False, error="引用目标不属于当前可回复事件")
    draft.reply_to_event_id = arguments.event_id
    return _json_result(True, data={"reply_to_event_id": arguments.event_id})


def apply_mentions(
    draft: ReplyDraft,
    raw_arguments: str,
    mentionable_user_ids: set[str],
) -> str:
    try:
        arguments = MentionMembersArguments.model_validate_json(raw_arguments)
    except ValidationError as error:
        return _json_result(False, error=f"工具参数校验失败: {error}")
    requested_user_ids = list(dict.fromkeys(arguments.user_ids))
    invalid_user_ids = [
        user_id for user_id in requested_user_ids if user_id not in mentionable_user_ids
    ]
    if invalid_user_ids:
        return _json_result(
            False,
            error="@目标不是当前群成员: " + ",".join(invalid_user_ids),
        )
    for user_id in requested_user_ids:
        if user_id not in draft.mention_user_ids:
            draft.mention_user_ids.append(user_id)
    return _json_result(True, data={"mention_user_ids": draft.mention_user_ids})


def reply_to_event_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": REPLY_TO_EVENT_TOOL_NAME,
            "description": (
                "设置最终群聊消息要引用的近期事件。参数使用运行时上下文中的 event_id；"
                "再次调用会覆盖之前的引用目标。此工具不会发送消息，调用后继续输出正文。"
            ),
            "parameters": strict_model_json_schema(ReplyToEventArguments),
            "strict": True,
        },
    }


def mention_members_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": MENTION_MEMBERS_TOOL_NAME,
            "description": (
                "把当前群成员加入最终消息的@目标，可一次指定多人并可多次调用。"
                "参数只使用已经确认的user_id。此工具不会发送消息，调用后继续输出正文。"
            ),
            "parameters": strict_model_json_schema(MentionMembersArguments),
            "strict": True,
        },
    }


def finish_without_reply_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": FINISH_WITHOUT_REPLY_TOOL_NAME,
            "description": (
                "结束本轮且不向群聊发送任何消息。仅在本轮系统提示明确允许终止，"
                "并且无需继续回复时调用。"
            ),
            "parameters": strict_model_json_schema(FinishWithoutReplyArguments),
            "strict": True,
        },
    }


def build_default_tool_registry(
    roster_service: GroupRosterService,
    memory_store: GroupMemoryStore,
    cli_runner: DockerCliRunner,
    tool_timeout_seconds: int,
    output_max_chars: int,
) -> ToolRegistry:
    registry = ToolRegistry(output_max_chars=output_max_chars)

    async def get_group_info(context: ToolContext, arguments: BaseModel) -> Any:
        roster = await roster_service.get_roster(context.group_id)
        return {
            "group_id": roster.group_id,
            "group_name": roster.group_name,
            "member_count": len(roster.members),
            "synced_at": roster.synced_at.isoformat(),
        }

    async def list_group_members(context: ToolContext, arguments: BaseModel) -> Any:
        roster = await roster_service.get_roster(context.group_id)
        return [
            {
                "user_id": member.user_id,
                "nickname": member.nickname,
                "card": member.card,
                "role": member.role,
            }
            for member in roster.members.values()
        ]

    async def get_group_member(context: ToolContext, arguments: BaseModel) -> Any:
        member_args = MemberArguments.model_validate(arguments.model_dump())
        member = await roster_service.get_member(
            context.bot,
            context.group_id,
            member_args.user_id,
            refresh=True,
        )
        if member is None:
            return {"found": False}
        return {"found": True, **member.model_dump(mode="json")}

    async def search_memory(context: ToolContext, arguments: BaseModel) -> Any:
        search_args = MemorySearchArguments.model_validate(arguments.model_dump())
        query = normalize_memory_text(search_args.query)
        memory = await memory_store.get(context.group_id)
        matches: List[Dict[str, Any]] = []
        for user_id, member in memory.members.items():
            for alias in member.learned_aliases:
                if query in normalize_memory_text(alias.value):
                    matches.append(
                        {"type": "alias", "user_id": user_id, "value": alias.value}
                    )
            for attribute in [*member.traits, *member.interests]:
                if query in normalize_memory_text(attribute.value):
                    matches.append(
                        {
                            "type": "member_attribute",
                            "user_id": user_id,
                            "value": attribute.value,
                        }
                    )
        for fact in memory.recent_facts:
            if query in normalize_memory_text(fact.content):
                matches.append(
                    {
                        "type": "fact",
                        "id": fact.id,
                        "content": fact.content,
                        "involved_user_ids": fact.involved_user_ids,
                        "last_confirmed_at": fact.last_confirmed_at.isoformat(),
                    }
                )
        return matches[:20]

    async def run_cli(context: ToolContext, arguments: BaseModel) -> Any:
        cli_args = CliArguments.model_validate(arguments.model_dump())
        result = await cli_runner.run(
            cli_args.command,
            context.workspace,
            timeout_seconds=cli_args.timeout_seconds,
        )
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }

    for definition in (
        ToolDefinition(
            name="get_current_group_info",
            description="获取当前群的名称、成员数量和成员快照更新时间。",
            arguments_model=EmptyArguments,
            executor=get_group_info,
            timeout_seconds=tool_timeout_seconds,
        ),
        ToolDefinition(
            name="list_current_group_members",
            description="列出当前群的成员 QQ 号、昵称、群名片和群角色。",
            arguments_model=EmptyArguments,
            executor=list_group_members,
            timeout_seconds=tool_timeout_seconds,
        ),
        ToolDefinition(
            name="get_current_group_member",
            description="通过 QQ user_id 获取当前群中某位成员的最新 OneBot 信息。",
            arguments_model=MemberArguments,
            executor=get_group_member,
            timeout_seconds=tool_timeout_seconds,
        ),
        ToolDefinition(
            name="search_group_memory",
            description="搜索当前群已学习的称呼、人物观察、兴趣和近期关键事实。",
            arguments_model=MemorySearchArguments,
            executor=search_memory,
            timeout_seconds=tool_timeout_seconds,
        ),
        ToolDefinition(
            name="run_cli",
            description=(
                "在隔离、无网络、一次性的 Linux 工作区中执行 shell、Python 或常用 CLI。"
            ),
            arguments_model=CliArguments,
            executor=run_cli,
            timeout_seconds=max(tool_timeout_seconds, 120),
        ),
    ):
        registry.register(definition)
    return registry


def _json_result(success: bool, data: Any = None, error: str = "") -> str:
    value: Dict[str, Any] = {"success": success}
    if success:
        value["data"] = data
    else:
        value["error"] = error
    return json.dumps(value, ensure_ascii=False, default=str)
