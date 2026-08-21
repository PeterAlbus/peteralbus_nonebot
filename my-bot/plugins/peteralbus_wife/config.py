from pathlib import Path
from typing import List

from pydantic import BaseModel, Field


class Config(BaseModel):
    """peteralbus-wife 插件配置。"""

    peteralbus_wife_res: str = "/home/PeterAlbus/napcat/resources/peteralbus_wife"
    peteralbus_wife_jm_option_path: str = str(Path(__file__).with_name("config.json"))
    peteralbus_wife_jm_work_dir: str = ""
    peteralbus_wife_jm_max_concurrency: int = 1
    peteralbus_wife_jm_download_timeout: int = 1800
    peteralbus_wife_jm_upload_timeout: int = 300
    peteralbus_wife_jm_failed_retention_hours: int = 24
    peteralbus_wife_jm_max_pdf_mb: int = 0
    peteralbus_wife_jm_allowed_groups: List[str] = Field(default_factory=list)
    peteralbus_wife_jm_allowed_users: List[str] = Field(default_factory=list)
