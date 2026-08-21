from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata

from .config import Config

from . import handler as _

__plugin_meta__ = PluginMetadata(
    name="multi_llm_chat",
    description="接入多模型API的群聊回复插件（DeepSeek / Xiaomi MiMo）",
    usage="自动处理未被其他插件处理的消息，支持按模型路由调用不同提供商生成回复",
    config=Config,
)

config = get_plugin_config(Config)
