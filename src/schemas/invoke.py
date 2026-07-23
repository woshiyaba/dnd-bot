"""通用 deep-agent 图调用接口模型。"""

from pydantic import BaseModel, Field


class InvokeRequest(BaseModel):
    """调用请求模型。"""

    user_input: str = Field(..., description="用户输入的消息")
    thread_id: str = Field(default="default", description="会话线程 ID")
    user_id: str = Field(default="用户ID", description="用户 ID")


class InvokeResponse(BaseModel):
    """模板图调用响应模型。"""

    user_input: str
    thread_id: str
    user_id: str
    result: str
