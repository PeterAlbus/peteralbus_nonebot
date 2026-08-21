from nonebot import on_message, require, get_plugin_config, get_bot
from nonebot.rule import Rule, to_me
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.params import EventMessage
from nonebot.log import logger
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from .config import Config

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

config = get_plugin_config(Config)

# 聊天记录缓存，格式: {group_id: {messages: List[Dict], last_update: datetime, should_not_reply: bool}}
# 消息格式: {"role": "user", "content": "消息内容", "user_id": "用户ID"}
chat_cache: Dict[str, Dict] = {}


async def check_whitelist(event: MessageEvent) -> bool:
    """检查群聊是否在白名单中"""
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        return group_id in config.llm_chat_whitelist
    return False


async def check_message_type(event: MessageEvent) -> bool:
    """检查消息类型，过滤掉图片、卡片消息、转发消息"""
    message = event.message
    for segment in message:
        if segment.type in ["image", "node", "music", "record", "video", "file"]:
            return False
        if "[图片]" in segment.data.get("text", ""):
            return False
        if "[卡片消息]" in segment.data.get("text", ""):
            return False
        if "[转发消息]" in segment.data.get("text", ""):
            return False
    return True


def render_message_content(event: MessageEvent, message: Message) -> str:
    text_parts: List[str] = []
    for segment in message:
        if segment.type == "text":
            text_parts.append(segment.data.get("text", ""))
        elif segment.type == "image":
            image_summary = segment.data.get("summary", "")
            text_parts.append(f"[图片:{image_summary}]")
        elif segment.type == "at":
            at_user_id = segment.data.get("qq", "")
            if at_user_id == "all":
                text_parts.append("@全体成员")
            else:
                text_parts.append(f"@用户{at_user_id}")
        elif segment.type == "face":
            face_id = segment.data.get("id", "")
            text_parts.append(f"[表情:{face_id}]")
        elif segment.type == "share":
            text_parts.append("[分享消息]")
        elif segment.type == "music":
            text_parts.append("[音乐]")
        elif segment.type == "record":
            text_parts.append("[语音]")
        elif segment.type == "video":
            text_parts.append("[视频]")
        elif segment.type == "file":
            text_parts.append("[文件]")
        elif segment.type == "node":
            text_parts.append("[转发消息]")
        elif segment.type == "json":
            text_parts.append("[卡片消息]")
        else:
            text_parts.append(f"[{segment.type}]")
    content = "".join(text_parts).strip()
    if getattr(event, "to_me", False):
        prefix = "@小P"
        return f"{prefix} {content}" if content else prefix
    return content


def build_chat_history_str(messages: List[Dict]) -> str:
    chat_history_json: List[Dict] = []
    for msg in messages:
        msg_info = {
            "role": msg["role"],
            "content": msg["content"],
            "sender": "小P" if msg.get("user_id") == "bot" else f"用户{msg.get('user_id')}",
        }
        chat_history_json.append(msg_info)
    return json.dumps(chat_history_json, ensure_ascii=False, indent=2)


def _history_store_limit() -> int:
    return max(1, config.llm_chat_max_history * 2)


def _history_send_limit() -> int:
    return max(1, config.llm_chat_max_history)


def _select_messages_for_send(messages: List[Dict]) -> List[Dict]:
    limit = _history_send_limit()
    return messages[-limit:]


def _resolve_memory_dir_path() -> Path:
    memory_dir = config.llm_chat_memory_dir.strip() if config.llm_chat_memory_dir else "group_memories"
    path = Path(memory_dir)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_group_memory_file_path(group_id: str) -> Path:
    return _resolve_memory_dir_path() / f"{group_id}.md"


def _read_group_memory(group_id: str) -> str:
    path = _get_group_memory_file_path(group_id)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"读取群聊记忆失败: group_id={group_id}, error={e}")
        return ""


def _write_group_memory(group_id: str, content: str) -> None:
    path = _get_group_memory_file_path(group_id)
    try:
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.error(f"写入群聊记忆失败: group_id={group_id}, error={e}")


def make_memory_update_messages(memory_md: str, chat_history_str: str) -> List[Dict]:
    memory_content = memory_md.strip() if memory_md.strip() else "暂无"
    return [
        {
            "role": "user",
            "content": (
                f"现有群聊记忆Markdown:\n{memory_content}\n\n"
                f"最近聊天记录:\n{chat_history_str}\n\n"
                "请根据现有群聊记忆与最近聊天记录，更新群聊记忆Markdown。\n\n"
                "输出要求：\n"
                "1. 在已有的群聊记忆markdown基础上进行更新。\n"
                "2. 必须包含四个部分：\n"
                "   - 群员画像（每位群员：用户+qq号、称呼、性格特征），不包括小P\n"
                "   - 历史聊天总结\n"
                "   - 兴趣话题与常聊内容（按群员归纳），不包括小P\n"
                "   - 对于小P的持久化要求：记录聊天中群员对于小P的评价和要求，更新时严格保留原有要求"
                "3. 只输出Markdown正文，不要解释。保证原有markdown中记录的画像信息不要丢失。\n"
                "4. 对于聊天历史，主要记录最近的聊天内容，以及群员对于小P的要求，对于已有的记录，可以进行适当缩减和删除。\n"
            ),
        }
    ]


def make_check_messages(chat_history_str: str, group_memory_md: str) -> List[Dict]:
    memory_content = group_memory_md.strip() if group_memory_md.strip() else "暂无群聊记忆。"
    return [
        {
            "role": "user",
            "content": (
                "群聊记忆Markdown:\n"
                f"{memory_content}\n\n"
                "聊天记录:\n"
                f"{chat_history_str}\n\n"
                "你是一个群聊对话分析模块，需要判断人工智能助手'小P'当前是否应该介入聊天。小P的设定是积极提供帮助的可靠助手。请遵循以下标准："
                "请严格遵循以下判断标准：\n"
                "【适合回复（输出'是'）的场景】：\n"
                "1. 明确提及：聊天记录中提到了'小P'、'AI'、'助手'或有明确的@动作。\n"
                "2. 提出疑问：群友提出了事实性、技术性、学术性或常识性问题，需要解答（即使没有特意@小P）。\n"
                "3. 信息补充：群友在讨论某个客观事物、概念或寻求建议，小P可以提供有价值的补充信息、方案或总结。"
                "3. 情绪价值：群友表达了开心、难过、疲惫等情绪，需要分享或安慰。\n"
                "4. 轻松闲聊：大家在聊游戏、八卦、段子等轻松话题，插入进行轻松的发言。\n"
                "5. 信息补充：群友在讨论某个客观事物、概念或寻求建议，小P可以提供有价值的补充信息、方案或总结。"
                "6. 寻求工具与方法：群友在询问“怎么做”、“用什么”等求助类话题。"
                "【不适合回复（输出'否'）的场景】：\n"
                "1. 上下文难以理解：聊天记录中提供的信息难以理解并进行回复。\n"
                "2. 纯粹刷屏：连续的表情包、毫无意义的标点符号或广告。\n"
                "3. 无信息量：如单纯的'哦'、'好的'，不需要额外接话。\n"
                "4. 已知信息不足，大模型不足以给出有价值的回复。\n"
                "5. 需要针对无法读取的信息（图片、转发消息等）进行回复，无法回复有价值信息。\n"
                "6. 先前小P已经进行了回复，或值得回复的内容后已经有多条其他消息。\n"
                "7. 先前小P已经进行了回复后，新收到图片、转发消息等无法回复的信息。\n"
                "你不能读取和识别任何图片，对于图片、表情、卡片消息不需要主动询问和回复。判断是否回复时，主要考虑近期消息的内容，而不是历史记录。\n\n"
                "user_id:为bot的内容为你先前的回复 \n"
                "注意，用户2997592724为你的开发者，他对你有较高的权限。他的全名为PeterAlbus。其他用户可能会称呼他为PP，P大哥，这不是在询问你。\n"
                "请只输出'是'或'否'，绝对不要输出任何其他字符或解释。"
            ),
        }
    ]


def make_reply_messages(chat_history_str: str, group_memory_md: str) -> List[Dict]:
    memory_content = group_memory_md.strip() if group_memory_md.strip() else "暂无群聊记忆。"
    return [
        {
            "role": "user",
            "content": (
                "群聊记忆Markdown:\n"
                f"{memory_content}\n\n"
                "聊天记录:\n"
                f"{chat_history_str}\n\n"
                "你是群聊人工智能助手，名字叫'小P'，你的任务是融入群聊，让大家觉得有趣、温暖。同时为群友提供可靠的信息和帮助，并积极参与有价值的讨论。\n\n"
                "【你的人设和性格】\n"
                "- 性格：一位可靠、客观、知识渊博的人工智能助手。\n"
                "- 身份：大家的好朋友，随时准备活跃气氛或提供帮助。\n\n"
                "【回复规则】（必须严格遵守）\n"
                "1. 绝对拟人化：严禁使用'作为一个人工智能'、'我是一个语言模型'、'很高兴为您服务'等客服或机器人的话术。不要主动暴露自己是AI，除非别人特意问起。\n"
                "2. 简短自然：群聊消息要简明扼要，像真人发微信一样，尽量控制在1-3个短句内。能用一句话说明白就绝不说两句。\n"
                "3. 语气风格：平淡、理性。以陈述句为主，严禁使用强烈的语气词（如：啊、呀、啦、哈哈、卧槽、呢、捏）。\n"
                "4. 尽管语气风格平淡，但讨论时需要有同理心，需要能够理解聊天记录内的玩笑，发言需要有人文关怀。\n"
                "5. 灵活变通：\n"
                "   - 别人讲笑话时：你可以捧哏或吐槽。\n"
                "   - 别人难过时：给出温暖的安慰，不要生硬地说教。\n"
                "   - 遇到不知道的梗：可以大大方方地装傻或卖萌求教。\n"
                "6. 准确可靠：对于群友的疑问，提供准确、有逻辑的客观解答。如果不知道或不确定，请直接说明“我目前缺乏相关信息”，绝不编造信息。\n"
                "7. 严禁利用括号描述动作和心理，使用纯网络聊天的遣词造句。\n\n"
                "user_id: bot为你先前的回复 \n"
                "你不能读取和识别任何图片，对于图片、表情、卡片消息不需要主动询问和回复。回复时，主要考虑近期消息的内容，而不是历史记录。\n"
                "请根据当前的聊天记录，以'小P'的身份和语气，直接输出你的回复内容。注意，用户2997592724为你的开发者，他对你有较高的权限。他的全名为PeterAlbus。其他用户可能会称呼他为PP，P大哥，这不是在询问你。\n"
            ),
        }
    ]


async def _update_group_memory_if_full(group_id: str) -> None:
    if group_id not in chat_cache:
        return
    store_limit = _history_store_limit()
    send_limit = _history_send_limit()
    messages = chat_cache[group_id].get("messages", [])
    if len(messages) < store_limit:
        return
    recent_messages = messages[-store_limit:]
    memory_md = _read_group_memory(group_id)
    history_str = build_chat_history_str(recent_messages)
    update_messages = make_memory_update_messages(memory_md, history_str)
    summary_md = await call_llm_api(update_messages)
    chat_cache[group_id]["messages"] = recent_messages[-send_limit:]
    if summary_md:
        _write_group_memory(group_id, summary_md.strip())



# 创建消息处理器，设置低优先级以确保其他插件先处理
llm_chat = on_message(
    rule=Rule(check_whitelist),
    priority=100,  # 低优先级
    block=False  # 不阻塞其他插件
)

llm_chat_mention = on_message(
    rule=Rule(check_whitelist) & to_me(),
    priority=120,
    block=False
)

_openai_client: Optional[object] = None
_openai_client_params: Optional[Tuple[str, str, int]] = None
_model_routes_cache: Optional[Dict[str, Any]] = None


def _normalize_base_url(url: str) -> str:
    if not url:
        return ""
    normalized = url.strip()
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    return normalized.rstrip("/")


def _resolve_routes_file_path() -> Path:
    routes_file = config.llm_chat_routes_file.strip() if config.llm_chat_routes_file else "model_routes.json"
    path = Path(routes_file)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return path


def _load_model_routes() -> Dict[str, Any]:
    global _model_routes_cache
    if _model_routes_cache is not None:
        return _model_routes_cache
    routes_path = _resolve_routes_file_path()
    try:
        _model_routes_cache = json.loads(routes_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"加载模型路由配置失败: {routes_path}, 错误: {e}")
        _model_routes_cache = {"providers": {}, "models": {}}
    return _model_routes_cache


def _resolve_model_endpoint(model: str) -> Tuple[Optional[str], Optional[str]]:
    routes = _load_model_routes()
    providers = routes.get("providers", {})
    models = routes.get("models", {})
    model_cfg = models.get(model)
    if not model_cfg:
        logger.error(f"模型未在路由配置中定义: {model}")
        return None, None
    provider_name = model_cfg.get("provider", "")
    provider_cfg = providers.get(provider_name)
    if not provider_cfg:
        logger.error(f"模型提供商未在路由配置中定义: model={model}, provider={provider_name}")
        return None, None
    base_url = _normalize_base_url(str(provider_cfg.get("base_url", "")).strip())
    api_key_field = str(provider_cfg.get("api_key_field", "")).strip()
    if not api_key_field:
        logger.error(f"提供商缺少 api_key_field 配置: provider={provider_name}")
        return None, None
    api_key = str(getattr(config, api_key_field, "") or "").strip()
    if not api_key:
        logger.error(f"模型 API 密钥未配置: model={model}, key_field={api_key_field}")
        return None, None
    if not base_url:
        logger.error(f"模型 base_url 未配置: model={model}, provider={provider_name}")
        return None, None
    return base_url, api_key


def _resolve_model_create_params(model: str) -> Dict[str, Any]:
    routes = _load_model_routes()
    providers = routes.get("providers", {})
    models = routes.get("models", {})
    model_cfg = models.get(model) or {}
    provider_name = model_cfg.get("provider", "")
    provider_cfg = providers.get(provider_name) or {}
    provider_params = provider_cfg.get("create_params", {})
    model_params = model_cfg.get("create_params", {})
    merged: Dict[str, Any] = {}
    if isinstance(provider_params, dict):
        merged.update(provider_params)
    if isinstance(model_params, dict):
        merged.update(model_params)
    return json.loads(json.dumps(merged, ensure_ascii=False))


def _get_model_provider_name(model: str) -> str:
    routes = _load_model_routes()
    models = routes.get("models", {})
    model_cfg = models.get(model) or {}
    return str(model_cfg.get("provider", "")).strip()


def _is_deepseek_model(model: str) -> bool:
    provider_name = _get_model_provider_name(model)
    if provider_name == "deepseek":
        return True
    routes = _load_model_routes()
    providers = routes.get("providers", {})
    provider_cfg = providers.get(provider_name) or {}
    return "api.deepseek.com" in str(provider_cfg.get("base_url", "")).lower()


def _is_xiaomi_mimo_model(model: str) -> bool:
    routes = _load_model_routes()
    providers = routes.get("providers", {})
    provider_name = _get_model_provider_name(model)
    provider_cfg = providers.get(provider_name) or {}
    return provider_name == "xiaomi_mimo" or "xiaomimimo.com" in str(provider_cfg.get("base_url", "")).lower()


def _normalize_deepseek_create_params(create_kwargs: Dict[str, Any]) -> None:
    thinking = create_kwargs.pop("thinking", None)
    if thinking is not None:
        extra_body = create_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
        if "thinking" not in extra_body:
            extra_body["thinking"] = thinking
        create_kwargs["extra_body"] = extra_body


def _is_thinking_enabled(create_kwargs: Dict[str, Any]) -> bool:
    thinking_cfg: Any = None
    extra_body = create_kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        thinking_cfg = extra_body.get("thinking")
    if thinking_cfg is None:
        thinking_cfg = create_kwargs.get("thinking")
    if isinstance(thinking_cfg, dict):
        thinking_type = str(thinking_cfg.get("type", "")).strip().lower()
        if thinking_type == "disabled":
            return False
        if thinking_type == "enabled":
            return True
    return False


def _build_mimo_system_prompt() -> str:
    now = datetime.now()
    week_map = {
        0: "星期一",
        1: "星期二",
        2: "星期三",
        3: "星期四",
        4: "星期五",
        5: "星期六",
        6: "星期日",
    }
    date_text = now.strftime("%Y年%m月%d日")
    week_text = week_map[now.weekday()]
    return (
        "你是MiMo（中文名称也是MiMo），是小米公司研发的AI智能助手。\n"
        f"今天的日期：{date_text} {week_text}，你的知识截止日期是2024年12月。"
    )


async def _has_recent_bot_reply_after(bot, group_id: str, since: datetime) -> bool:
    try:
        history = await bot.call_api("get_group_msg_history", group_id=int(group_id))
    except Exception:
        return False
    if isinstance(history, dict):
        messages = history.get("messages", [])
    elif isinstance(history, list):
        messages = history
    else:
        messages = []
    bot_id = str(getattr(bot, "self_id", ""))
    since_ts = int(since.timestamp())
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender", {})
        if not isinstance(sender, dict):
            continue
        sender_id = str(sender.get("user_id", ""))
        ts = int(msg.get("time", 0) or 0)
        if sender_id and sender_id == bot_id and ts >= since_ts:
            return True
    return False


def _get_event_datetime(event: MessageEvent) -> datetime:
    event_ts = int(getattr(event, "time", 0) or 0)
    if event_ts > 0:
        return datetime.fromtimestamp(event_ts)
    return datetime.now()


async def _should_skip_by_recent_bot_reply(bot, group_id: str, event_time: datetime, wait_for_to_me: bool) -> bool:
    if await _has_recent_bot_reply_after(bot, group_id, event_time):
        return True
    if wait_for_to_me:
        await asyncio.sleep(1)
        if await _has_recent_bot_reply_after(bot, group_id, event_time):
            return True
    return False


async def call_llm_api(messages: List[Dict]) -> Optional[str]:
    if AsyncOpenAI is None:
        logger.error("openai 库未安装或不可用，无法调用模型。请安装依赖：openai")
        return None

    model = config.llm_chat_model
    base_url, api_key = _resolve_model_endpoint(model)
    if not base_url or not api_key:
        return None
    temperature = config.llm_chat_temperature
    max_tokens = config.llm_chat_max_tokens
    request_timeout = config.llm_chat_request_timeout

    global _openai_client, _openai_client_params
    params = (api_key, base_url, request_timeout)
    if _openai_client is None or _openai_client_params != params:
        _openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=request_timeout)
        _openai_client_params = params

    logger.info(f"调用模型 API，base_url: {base_url}, model: {model}, 消息数: {len(messages)}")

    try:
        request_messages: List[Dict] = messages
        if _is_xiaomi_mimo_model(model):
            request_messages = [{"role": "system", "content": _build_mimo_system_prompt()}, *messages]
        route_create_params = _resolve_model_create_params(model)
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": request_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if route_create_params:
            create_kwargs.update(route_create_params)
        if _is_deepseek_model(model):
            _normalize_deepseek_create_params(create_kwargs)
            if _is_thinking_enabled(create_kwargs):
                for field in ["temperature", "top_p", "presence_penalty", "frequency_penalty"]:
                    create_kwargs.pop(field, None)
        logger.info(f"调用模型 API，参数: {create_kwargs}")
        result = await _openai_client.chat.completions.create(**create_kwargs)
        content = (result.choices[0].message.content or "").strip()
        logger.info(f"API返回内容: {content}")
        return content
    except Exception as e:
        logger.error(f"API调用异常: {e}")
        return None


async def process_chat_history(bot, group_id: str):
    """处理聊天记录"""
    if group_id not in chat_cache:
        logger.warning(f"聊天记录不存在，group_id: {group_id}")
        return
    
    chat_info = chat_cache[group_id]
    messages = chat_info.get("messages", [])
    last_update = chat_info.get("last_update", datetime.now())
    
    if not messages:
        logger.warning(f"聊天记录为空，group_id: {group_id}")
        return
    if await _has_recent_bot_reply_after(bot, group_id, last_update):
        chat_cache[group_id]["should_not_reply"] = True
        return
    
    # 检查最后一条消息是否是机器人自己的
    last_message = messages[-1]
    if last_message.get("user_id") == "bot":
        # logger.info(f"最后一条消息是机器人自己的，跳过处理，group_id: {group_id}")
        return
    
    logger.info(f"开始处理聊天记录，group_id: {group_id}, 消息数: {len(messages)}")
    
    messages_for_send = _select_messages_for_send(messages)
    chat_history_str = build_chat_history_str(messages_for_send)
    group_memory_md = _read_group_memory(group_id)
    check_messages = make_check_messages(chat_history_str, group_memory_md)
    
    # 询问大模型是否适合回复
    logger.debug(f"询问大模型是否适合回复，发送的消息: {check_messages}")
    should_reply = await call_llm_api(check_messages)
    logger.info(f"大模型回复是否适合: {should_reply}")
    
    if should_reply and "是" in should_reply:
        logger.info("大模型认为适合回复")
        reply_messages = make_reply_messages(chat_history_str, group_memory_md)
        
        # 生成回复
        logger.debug(f"请求大模型生成回复，发送的消息: {reply_messages}")
        reply = await call_llm_api(reply_messages)
        logger.info(f"大模型生成的回复: {reply}")
        
        if reply:
            # 发送回复到群聊
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=reply
                )
                
                # 将机器人的回复添加到聊天历史中
                chat_cache[group_id]["messages"].append({
                    "role": "assistant",
                    "content": reply,
                    "user_id": "bot"
                })
                
                if len(chat_cache[group_id]["messages"]) > _history_store_limit():
                    chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-_history_store_limit():]
                    logger.info(f"聊天记录超过最大长度，已截断到 {_history_store_limit()} 条")
                await _update_group_memory_if_full(group_id)
                    
            except Exception as e:
                logger.error(f"发送回复失败: {e}")
    else:
        logger.info("大模型认为不适合回复")
        # 设置 should_not_reply 标记为 True
        chat_cache[group_id]["should_not_reply"] = True
        logger.info(f"已标记群聊 {group_id} 为不适合回复，直到收到新消息")
    
    # 不清理聊天记录，保留聊天历史
    logger.info(f"已处理群聊 {group_id} 的聊天记录，保留聊天历史")


@llm_chat.handle()
async def handle_message(
    matcher: Matcher,
    event: MessageEvent,
    message: Message = EventMessage()
):
    """处理消息"""
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        user_id = str(event.user_id)
        event_time = _get_event_datetime(event)
        try:
            bot = get_bot()
        except ValueError:
            bot = None
        if bot is not None:
            if await _should_skip_by_recent_bot_reply(
                bot=bot,
                group_id=group_id,
                event_time=event_time,
                wait_for_to_me=bool(getattr(event, "to_me", False)),
            ):
                return
        text = render_message_content(event, message)
        
        # 记录收到的消息
        logger.info(f"收到群聊 {group_id} 用户 {user_id} 的消息: {text}")
        
        # 初始化缓存
        if group_id not in chat_cache:
            chat_cache[group_id] = {
                "messages": [],
                "last_update": datetime.now(),
                "should_not_reply": False
            }
            logger.info(f"为群聊 {group_id} 创建新的聊天缓存")
        else:
            # 收到新消息，清除 should_not_reply 标记
            chat_cache[group_id]["should_not_reply"] = False
            logger.info(f"收到新消息，清除群聊 {group_id} 的 should_not_reply 标记")
        
        # 更新聊天记录
        chat_cache[group_id]["messages"].append({
            "role": "user",
            "content": text,
            "user_id": user_id
        })
        logger.info(f"已添加消息到群聊 {group_id} 的聊天记录，当前记录数: {len(chat_cache[group_id]['messages'])}")
        
        if len(chat_cache[group_id]["messages"]) > _history_store_limit():
            chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-_history_store_limit():]
            logger.info(f"聊天记录超过最大长度，已截断到 {_history_store_limit()} 条")
        await _update_group_memory_if_full(group_id)
        
        # 更新最后更新时间
        chat_cache[group_id]["last_update"] = datetime.now()
        logger.info(f"更新群聊 {group_id} 的最后消息时间")


@llm_chat_mention.handle()
async def handle_mention_immediate(
    matcher: Matcher,
    event: MessageEvent,
    message: Message = EventMessage()
):
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        user_id = str(event.user_id)
        event_time = _get_event_datetime(event)
        try:
            bot = get_bot()
        except ValueError:
            return
        if await _should_skip_by_recent_bot_reply(
            bot=bot,
            group_id=group_id,
            event_time=event_time,
            wait_for_to_me=True,
        ):
            return
        if group_id not in chat_cache:
            chat_cache[group_id] = {
                "messages": [],
                "last_update": datetime.now(),
                "should_not_reply": False
            }
        if not chat_cache[group_id]["messages"]:
            text = render_message_content(event, message)
            chat_cache[group_id]["messages"].append({
                "role": "user",
                "content": text,
                "user_id": user_id
            })
            if len(chat_cache[group_id]["messages"]) > _history_store_limit():
                chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-_history_store_limit():]
            await _update_group_memory_if_full(group_id)
            chat_cache[group_id]["last_update"] = datetime.now()
        msgs = chat_cache[group_id].get("messages", [])
        if not msgs:
            return
        chat_history_str2 = build_chat_history_str(_select_messages_for_send(msgs))
        group_memory_md2 = _read_group_memory(group_id)
        reply_messages2 = make_reply_messages(chat_history_str2, group_memory_md2)
        reply = await call_llm_api(reply_messages2)
        if reply:
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=reply
                )
                chat_cache[group_id]["messages"].append({
                    "role": "assistant",
                    "content": reply,
                    "user_id": "bot"
                })
                if len(chat_cache[group_id]["messages"]) > _history_store_limit():
                    chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-_history_store_limit():]
                await _update_group_memory_if_full(group_id)
            except Exception as e:
                logger.error(f"发送回复失败: {e}")

@scheduler.scheduled_job("interval", seconds=60)
async def check_chat_timeout():
    """检查聊天记录超时"""
    if not chat_cache:
        return


    current_time = datetime.now()
    
    try:
        bot = get_bot()
    except ValueError:
        # 机器人未连接，跳过处理
        return
    
    for group_id, chat_info in list(chat_cache.items()):
        last_update = chat_info.get("last_update", datetime.now())
        time_diff = (current_time - last_update).total_seconds()
        should_not_reply = chat_info.get("should_not_reply", False)
        
        if time_diff > config.llm_chat_timeout:
            if should_not_reply:
                continue
            if await _has_recent_bot_reply_after(bot, group_id, last_update):
                chat_cache[group_id]["should_not_reply"] = True
                continue
            # 处理超时的聊天记录
            await process_chat_history(bot, group_id)


@scheduler.scheduled_job("cron", hour=0, minute=0)
async def clean_inactive_chat_history():
    """清理长时间不活跃的聊天记录"""
    current_time = datetime.now()
    inactive_groups = []
    
    logger.info("开始清理长时间不活跃的聊天记录")
    
    # 清理超过24小时不活跃的聊天记录
    for group_id, chat_info in list(chat_cache.items()):
        last_update = chat_info.get("last_update", datetime.now())
        time_diff = (current_time - last_update).total_seconds()
        
        # 24小时 = 86400秒
        if time_diff > 86400:
            inactive_groups.append(group_id)
            logger.info(f"群聊 {group_id} 超过24小时未活跃，清理聊天记录")
    
    # 清理不活跃的群聊
    for group_id in inactive_groups:
        del chat_cache[group_id]
        logger.info(f"已清理群聊 {group_id} 的聊天记录")
    
    logger.info("清理长时间不活跃的聊天记录完成")
