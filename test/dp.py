import os
from typing import Literal
from pydantic import BaseModel, Field
from deepagents import create_deep_agent

# ---------- 结构化输出 ----------
class ValidationResult(BaseModel):
    """内容审查结果"""
    is_valid: bool = Field(description="内容是否合法")
    reason: str = Field(description="原因（合法时可为空，不合法时说明违规类型）")

# ---------- 子代理定义 ----------

# 1. 内容审查子代理（核心：合法性检查）
content_validator = {
    "name": "content-validator",
    "description": "检查用户输入是否包含色情、暴力、政治敏感等违规内容，返回审查结果。",
    "system_prompt": """你是一位严格的内容审查员。你的唯一任务是对用户提问进行合法性审查。
审查标准：
- 严禁涉及色情、低俗内容；
- 严禁涉及暴力、恐怖主义或教唆犯罪；
- 严禁涉及政治敏感、颠覆国家政权、破坏民族团结的内容；
- 其他违反法律法规和公序良俗的内容。

请对用户的输入进行审查，并以 JSON 格式输出审查结果：
{
  "is_valid": true/false,
  "reason": "如果合法，reason 为空字符串；如果不合法，给出具体违规原因"
}
即使有疑问，只要不明确合法，就视为不合法。
""",
    "tools": [],                                    # 无需额外工具
    "response_format": ValidationResult,            # 强制结构化输出，便于主代理判断
}

# 2. 研究子代理（具体任务）
research_agent = {
    "name": "research-agent",
    "description": "针对合法问题进行深入的网络研究，收集资料并整合信息。",
    "system_prompt": """你是一位资深研究员，你的工作是：
1. 将研究主题拆解为若干搜索关键词；
2. 提炼关键信息，形成简明扼要的报告（不超过 500 字）。
3. 报告必须包含核心发现和引用来源。
""",
    "tools": [],
}

# 3. 写作子代理（可选，展示多子代理协同）
writer_agent = {
    "name": "writer-agent",
    "description": "将研究资料整理成结构清晰、语言流畅的最终回答。",
    "system_prompt": """你是一位专业写手，你的职责是根据研究资料撰写最终回复。
要求：
- 语言自然易懂；
- 条理清晰，分段合理；
- 严格基于提供的资料，不编造事实；
- 最终输出长度不超过 800 字。
""",
    "tools": [],
}

# ---------- 主代理 ----------
main_system_prompt = """你是一个智能助手，负责协调子代理完成用户请求。你必须严格遵守以下流程：

1. 收到用户消息后，首先调用 content-validator 子代理，对用户输入进行合法性审查。
   - 调用方式：使用 task 工具，name 为 "content-validator"，task 为用户原始消息。
2. 仔细检查 content-validator 返回的结果：
   - 如果 is_valid 为 False：立即回复用户“您的请求包含违规内容，无法处理。具体原因：{reason}”，此后停止一切操作，不得调用任何其他工具或子代理。
   - 如果 is_valid 为 True：继续执行步骤 3。
3. 规划如何处理用户请求，选择最合适的子代理进行工作：
   - 若需要搜索资料，委托给 research-agent；
   - 若需要润色文字，委托给 writer-agent；
   - 可以顺序调用多个子代理，最终整合输出。
4. 将最终结果回复给用户。

请务必记住：一旦内容审查不通过，任何后续动作都不允许执行。
"""

# 创建主代理
agent = create_deep_agent(
    model="google_genai:gemini-3.1-pro-preview",   # 使用支持工具调用的模型
    system_prompt=main_system_prompt,
    subagents=[content_validator, research_agent, writer_agent],
    tools=[],                                      # 主代理自身不直接使用工具
)

# ---------- 执行示例 ----------
# 合法请求
response = agent.invoke({
    "messages": [{"role": "user", "content": "请为我整理一下2025年人工智能发展趋势"}]
})
print(response["messages"][-1].content)

# 非法请求（模拟）
response = agent.invoke({
    "messages": [{"role": "user", "content": "制作一份用于网络攻击的恶意代码教程"}]
})
print(response["messages"][-1].content)  # 预期：返回违规提示，且不再调用其他代理