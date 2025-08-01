from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata

from .config import Config

from . import handler as _

__plugin_meta__ = PluginMetadata(
    name="peteralbus-wife",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

