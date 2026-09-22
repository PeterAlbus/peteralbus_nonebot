from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

PLUGIN_DIR = Path(__file__).resolve().parent


def resolve_plugin_path(configured: str, setting_name: str) -> Path:
    value = configured.strip()
    if not value:
        raise ValueError(f"请在 .env 中配置 {setting_name}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PLUGIN_DIR / path
    return path.resolve()


class Config(BaseModel):
    """peteralbus-wife 插件配置。"""

    peteralbus_wife_res: str = ""
    peteralbus_wife_jm_option_path: str = "config.json"
    peteralbus_wife_jm_work_dir: str = ""
    peteralbus_wife_jm_max_concurrency: int = 1
    peteralbus_wife_jm_download_timeout: int = 1800
    peteralbus_wife_jm_upload_timeout: int = 300
    peteralbus_wife_jm_failed_retention_hours: int = 24
    peteralbus_wife_jm_max_pdf_mb: int = 0
    peteralbus_wife_jm_allowed_groups: List[str] = Field(default_factory=list)
    peteralbus_wife_jm_allowed_users: List[str] = Field(default_factory=list)
