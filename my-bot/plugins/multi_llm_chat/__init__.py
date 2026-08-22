from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata

from . import handler
from .config import Config

__all__ = ("config", "handler")

__plugin_meta__ = PluginMetadata(
    name="multi_llm_chat",
    description="带工具调用、结构化上下文和群聊记忆的多模型群聊助手",
    usage=(
        "处理白名单群消息；支持 DeepSeek/MiMo、OneBot 群成员同步、"
        "JSON 记忆和隔离 CLI 工具"
    ),
    config=Config,
)

config = get_plugin_config(Config)
