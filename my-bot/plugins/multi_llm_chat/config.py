from pydantic import BaseModel
from typing import List, Optional


class Config(BaseModel):
    """插件配置"""
    llm_chat_deepseek_api_key: str = ""
    llm_chat_xiaomi_mimo_api_key: str = ""

    llm_chat_model: str = "deepseek-chat"
    llm_chat_temperature: float = 0.7
    llm_chat_max_tokens: int = 4096
    llm_chat_request_timeout: int = 60
    llm_chat_routes_file: str = "model_routes.json"
    llm_chat_memory_dir: str = "group_memories"
    
    # 群聊白名单
    llm_chat_whitelist: List[str] = []
    
    # 聊天记录配置
    llm_chat_timeout: int = 10  # 聊天记录超时时间（秒）
    llm_chat_max_history: int = 15  # 最大聊天记录条数
