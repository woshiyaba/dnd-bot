"""
JSON 解析工具。

放一些和业务无关的通用解析逻辑，避免占用 graph 编排文件。
"""

from __future__ import annotations

import json
import re


def extract_json_array(text: str) -> list[str]:
    """
    从文本中提取 JSON 数组，处理 LLM 可能添加的额外内容。

    支持的格式：
    - 纯 JSON 数组：["a", "b"]
    - markdown 包裹：```json\n["a"]\n```
    - 带前后缀文字：以下是话术：["a"] 希望有帮助

    Returns:
        提取到的字符串列表，解析失败则将原文包装成单元素列表
    """
    if not text or not text.strip():
        return []

    normalized_text = text.strip()

    code_block_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    code_block_match = re.search(code_block_pattern, normalized_text)
    if code_block_match:
        normalized_text = code_block_match.group(1).strip()

    try:
        result = json.loads(normalized_text)
        if isinstance(result, list):
            return [str(item) for item in result]
    except json.JSONDecodeError:
        pass

    # 贪婪匹配：第一个 [ 到最后一个 ]，处理前缀垃圾（与 extract_json_object 策略一致）
    greedy_pattern = r"\[[\s\S]*\]"
    greedy_match = re.search(greedy_pattern, normalized_text)
    if greedy_match:
        try:
            result = json.loads(greedy_match.group(0))
            if isinstance(result, list) and result:
                return [str(item) for item in result]
        except json.JSONDecodeError:
            pass

    # non-greedy 逐段尝试（兜底处理嵌套方括号场景）
    lazy_pattern = r"\[[\s\S]*?\]"
    for match_text in re.findall(lazy_pattern, normalized_text):
        try:
            result = json.loads(match_text)
            if isinstance(result, list) and result:
                return [str(item) for item in result]
        except json.JSONDecodeError:
            continue

    return [normalized_text]


def extract_json_object(text: str) -> dict | None:
    """
    从文本中提取 JSON 对象，处理 LLM 可能添加的额外内容。

    支持的格式：
    - 纯 JSON 对象：{"key": "value"}
    - markdown 包裹：```json\n{"key": "value"}\n```
    - 带前后缀文字

    Returns:
        解析到的字典，解析失败返回 None
    """
    if not text or not text.strip():
        return None

    normalized_text = text.strip()

    code_block_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    code_block_match = re.search(code_block_pattern, normalized_text)
    if code_block_match:
        normalized_text = code_block_match.group(1).strip()

    try:
        result = json.loads(normalized_text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 尝试匹配最外层的 { ... }
    brace_pattern = r"\{[\s\S]*\}"
    match = re.search(brace_pattern, normalized_text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None
