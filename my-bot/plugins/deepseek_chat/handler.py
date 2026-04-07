from math import log
from nonebot import on_message, require, get_plugin_config, get_bot
from nonebot.rule import Rule, to_me
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.params import EventMessage
from nonebot.log import logger
import asyncio
import aiohttp
import json
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from .config import Config

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
        return group_id in config.deepseek_chat_whitelist
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


def make_check_messages(chat_history_str: str) -> List[Dict]:
    return [
        {
            "role": "user",
            "content": (
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
                ". 寻求工具与方法：群友在询问“怎么做”、“用什么”等求助类话题。"
                "【不适合回复（输出'否'）的场景】：\n"
                "1. 上下文难以理解：聊天记录中提供的信息难以理解并进行回复。\n"
                "2. 纯粹刷屏：连续的表情包、毫无意义的标点符号或广告。\n"
                "3. 无信息量：如单纯的'哦'、'好的'，不需要额外接话。\n"
                "参与聊天可以适当积极，讨论中也可以略微插嘴。\n\n"
                "请只输出'是'或'否'，绝对不要输出任何其他字符或解释。"
            ),
        }
    ]


def make_reply_messages(chat_history_str: str) -> List[Dict]:
    return [
        {
            "role": "user",
            "content": (
                "聊天记录:\n"
                f"{chat_history_str}\n\n"
                "你是群聊人工智能助手，名字叫'小P'，你的任务是融入群聊，让大家觉得有趣、温暖。同时为群友提供可靠的信息和帮助，并积极参与有价值的讨论。\n\n"
                "【你的人设和性格】\n"
                "- 性格：一位可靠、客观、知识渊博的人工智能助手。\n"
                "- 身份：大家的好朋友，随时准备活跃气氛或提供帮助。\n\n"
                "【回复规则】（必须严格遵守）\n"
                "1. 绝对拟人化：严禁使用'作为一个人工智能'、'我是一个语言模型'、'很高兴为您服务'等客服或机器人的话术。不要主动暴露自己是AI，除非别人特意问起。\n"
                "2. 简短自然：群聊消息要简明扼要，像真人发微信一样，尽量控制在1-3个短句内。能用一句话说明白就绝不说两句。\n"
                "3. 语气风格：平淡、理性、克制。以陈述句为主，严禁使用强烈的语气词（如：啊、呀、啦、哈哈、卧槽、呢、捏）。\n"
                "4. 灵活变通：\n"
                "   - 别人讲笑话时：你可以捧哏或吐槽。\n"
                "   - 别人难过时：给出温暖的安慰，不要生硬地说教。\n"
                "   - 遇到不知道的梗：可以大大方方地装傻或卖萌求教。\n"
                "5. 准确可靠：对于群友的疑问，提供准确、有逻辑的客观解答。如果不知道或不确定，请直接说明“我目前缺乏相关信息”，绝不编造"
                "6. 严禁利用括号描述动作和心理，使用纯网络聊天的遣词造句。\n\n"
                "请根据当前的聊天记录，以'小P'的身份和语气，直接输出你的回复内容。注意，用户2997592724为你的开发者，他对你有较高的权限。他的全名为PeterAlbus。\n"
            ),
        }
    ]



# 创建消息处理器，设置低优先级以确保其他插件先处理
deepseek_chat = on_message(
    rule=Rule(check_whitelist),
    priority=100,  # 低优先级
    block=False  # 不阻塞其他插件
)

deepseek_chat_mention = on_message(
    rule=Rule(check_whitelist) & to_me(),
    priority=120,
    block=False
)


async def call_deepseek_api(messages: List[Dict]) -> Optional[str]:
    """调用DeepSeek API"""
    if not config.deepseek_api_key:
        logger.error("DeepSeek API 密钥未配置")
        return None
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.deepseek_api_key}"
    }
    
    data = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1024
    }
    
    logger.info(f"调用DeepSeek API，消息数: {len(messages)}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config.deepseek_api_url,
                headers=headers,
                data=json.dumps(data)
            ) as response:
                logger.info(f"API响应状态码: {response.status}")
                
                if response.status == 200:
                    result = await response.json()
                    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                    logger.info(f"API返回内容: {content}")
                    return content
                else:
                    logger.error(f"API调用失败，状态码: {response.status}")
                    return None
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
    
    if not messages:
        logger.warning(f"聊天记录为空，group_id: {group_id}")
        return
    
    # 检查最后一条消息是否是机器人自己的
    last_message = messages[-1]
    if last_message.get("user_id") == "bot":
        # logger.info(f"最后一条消息是机器人自己的，跳过处理，group_id: {group_id}")
        return
    
    logger.info(f"开始处理聊天记录，group_id: {group_id}, 消息数: {len(messages)}")
    
    chat_history_str = build_chat_history_str(messages)
    check_messages = make_check_messages(chat_history_str)
    
    # 询问大模型是否适合回复
    logger.debug(f"询问大模型是否适合回复，发送的消息: {check_messages}")
    should_reply = await call_deepseek_api(check_messages)
    logger.info(f"大模型回复是否适合: {should_reply}")
    
    if should_reply and "是" in should_reply:
        logger.info("大模型认为适合回复")
        reply_messages = make_reply_messages(chat_history_str)
        
        # 生成回复
        logger.debug(f"请求大模型生成回复，发送的消息: {reply_messages}")
        reply = await call_deepseek_api(reply_messages)
        logger.info(f"大模型生成的回复: {reply}")
        
        if reply:
            # 发送回复到群聊
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=reply
                )
                logger.info(f"DeepSeek回复已发送到群聊 {group_id}: {reply}")
                
                # 将机器人的回复添加到聊天历史中
                chat_cache[group_id]["messages"].append({
                    "role": "assistant",
                    "content": reply,
                    "user_id": "bot"
                })
                logger.info(f"已将机器人回复添加到群聊 {group_id} 的聊天历史中")
                
                # 限制聊天记录长度
                if len(chat_cache[group_id]["messages"]) > config.deepseek_chat_max_history:
                    chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-config.deepseek_chat_max_history:]
                    logger.info(f"聊天记录超过最大长度，已截断到 {config.deepseek_chat_max_history} 条")
                    
            except Exception as e:
                logger.error(f"发送回复失败: {e}")
    else:
        logger.info("大模型认为不适合回复")
        # 设置 should_not_reply 标记为 True
        chat_cache[group_id]["should_not_reply"] = True
        logger.info(f"已标记群聊 {group_id} 为不适合回复，直到收到新消息")
    
    # 不清理聊天记录，保留聊天历史
    logger.info(f"已处理群聊 {group_id} 的聊天记录，保留聊天历史")


@deepseek_chat.handle()
async def handle_message(
    matcher: Matcher,
    event: MessageEvent,
    message: Message = EventMessage()
):
    """处理消息"""
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        user_id = str(event.user_id)
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
        
        # 限制聊天记录长度
        if len(chat_cache[group_id]["messages"]) > config.deepseek_chat_max_history:
            chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-config.deepseek_chat_max_history:]
            logger.info(f"聊天记录超过最大长度，已截断到 {config.deepseek_chat_max_history} 条")
        
        # 更新最后更新时间
        chat_cache[group_id]["last_update"] = datetime.now()
        logger.info(f"更新群聊 {group_id} 的最后消息时间")


@deepseek_chat_mention.handle()
async def handle_mention_immediate(
    matcher: Matcher,
    event: MessageEvent,
    message: Message = EventMessage()
):
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        user_id = str(event.user_id)
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
            if len(chat_cache[group_id]["messages"]) > config.deepseek_chat_max_history:
                chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-config.deepseek_chat_max_history:]
            chat_cache[group_id]["last_update"] = datetime.now()
        msgs = chat_cache[group_id].get("messages", [])
        if not msgs:
            return
        chat_history_str2 = build_chat_history_str(msgs)
        reply_messages2 = make_reply_messages(chat_history_str2)
        reply = await call_deepseek_api(reply_messages2)
        if reply:
            try:
                bot = get_bot()
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
                if len(chat_cache[group_id]["messages"]) > config.deepseek_chat_max_history:
                    chat_cache[group_id]["messages"] = chat_cache[group_id]["messages"][-config.deepseek_chat_max_history:]
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
        
        if time_diff > config.deepseek_chat_timeout:
            if should_not_reply:
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
