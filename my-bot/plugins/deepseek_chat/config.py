from pydantic import BaseModel
from typing import List, Optional


class Config(BaseModel):
    """插件配置"""
    # DeepSeek API 配置
    deepseek_api_key: str = ""
    deepseek_api_url: str = "https://api.deepseek.com/v1/chat/completions"
    
    # 群聊白名单
    deepseek_chat_whitelist: List[str] = []
    
    # 聊天记录配置
    deepseek_chat_timeout: int = 10  # 聊天记录超时时间（秒）
    deepseek_chat_max_history: int = 15  # 最大聊天记录条数
