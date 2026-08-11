"""chat 相关请求契约"""
from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    source_id: str = ""
    # 人机协同 (P2): 用户对 confirm_required 事件的回复
    # "continue" = 继续用已有分析结果 / "reanalyze" = 重新分析 / 空 = 正常新请求
    confirmation: str = ""
