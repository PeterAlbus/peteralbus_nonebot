from typing import List, Optional

from pydantic import BaseModel, Field


class Config(BaseModel):
    """插件配置"""

    llm_chat_deepseek_api_key: str = ""
    llm_chat_xiaomi_mimo_api_key: str = ""

    llm_chat_model: str = "deepseek-v4-flash"
    llm_chat_temperature: float = 0.7
    llm_chat_max_tokens: int = 4096
    llm_chat_request_timeout: int = 60
    llm_chat_image_understanding: Optional[bool] = None
    llm_chat_routes_file: str = "model_routes.json"
    llm_chat_state_dir: str = "state"
    llm_chat_identity_config_file: str = "identity_config.json"
    llm_chat_raw_request_log_dir: str = "llm_request_logs"
    llm_chat_raw_request_retention_days: int = 7

    llm_chat_whitelist: List[str] = Field(default_factory=list)

    llm_chat_timeout: int = 10
    llm_chat_reply_grace_seconds: float = 0.5
    llm_chat_context_char_budget: int = 24000
    llm_chat_recent_event_min_count: int = 8
    llm_chat_conversation_max_events: int = 80
    llm_chat_summary_max_chars: int = 6000
    llm_chat_memory_max_facts: int = 64
    llm_chat_memory_max_aliases_per_member: int = 8
    llm_chat_roster_refresh_seconds: int = 21600

    llm_chat_max_tool_steps: int = 4
    llm_chat_tool_timeout_seconds: int = 30
    llm_chat_tool_output_max_chars: int = 12000
    llm_chat_custom_tools_module: str = ""

    llm_chat_cli_image: str = "peteralbus-nonebot-tool-runner:latest"
    llm_chat_cli_timeout_seconds: int = 20
    llm_chat_cli_output_max_chars: int = 12000
    llm_chat_cli_workspace_dir: str = "tool_workspaces"
