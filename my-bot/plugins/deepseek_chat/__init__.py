from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata

from .config import Config

from . import handler as _

__plugin_meta__ = PluginMetadata(
    name="deepseek_chat",
    description="接入DeepSeek大模型API的聊天插件",
    usage="自动处理未被其他插件处理的消息，使用DeepSeek大模型生成回复",
    config=Config,
)

config = get_plugin_config(Config)
