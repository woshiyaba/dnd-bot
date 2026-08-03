# LLM JSON 解析失败恢复方案

> 文档状态：计划实施
> 设计基线：2026-08-03
> 单一问题：LLM 输出无法解析时，当前流程直接失败，无法进入后续修复
> 目标文件：`src/story/generator.py`、`src/common/utils/json_parser.py`、`src/common/utils/llm_util.py`

## 1. 问题定义

当前 `_complete_json()` 调用模型后立即执行 `extract_json_object()`。如果输出被截断、包含不完整 JSON 或返回非对象内容，会直接抛出 `StoryGenerationError`。StoryPlan 和分片的业务修复循环都拿不到 `dict`，因此没有修复机会。

本方案只解决“文本到 JSON 对象”的恢复，不处理对象内部的业务合法性。

## 2. 不采用的做法

- 不用正则猜测缺失字段值。
- 不为截断结果自动补大括号后直接当作有效故事。
- 不把无法解析的内容保存为 validated artifact。
- 不无限重发同一个大 Prompt。
- 不把完整原始响应默认写进生产日志。

代码可以识别和分类错误，但只有真实 AI 可以补写缺失的创作内容。

## 3. 统一返回对象

将模型调用和 JSON 提取解耦：

```python
@dataclass(slots=True)
class LLMTextCompletion:
    stage: str
    model_name: str
    text: str
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    elapsed_ms: int


@dataclass(slots=True)
class JSONExtractionResult:
    value: dict[str, Any] | None
    error_kind: Literal["none", "empty", "truncated", "malformed", "non_object"]
    error_detail: str | None
```

`_complete_text()` 只负责真实模型调用和元数据；`extract_json_result()` 只负责确定性解析与分类。

## 4. 固定恢复流程

```text
调用模型
  -> 收集 finish_reason、长度和 token
  -> 提取 JSON
      -> 成功：进入 Pydantic/业务校验
      -> 网络或超时：按传输预算重试
      -> 明确截断：提高本单元输出预算后重新生成 1 次
      -> 完整但语法错误：调用格式修复模型 1 次
      -> 仍失败：该单元显式失败
```

### 4.1 截断和语法错误必须分开

优先使用供应商返回的 `finish_reason`：

- `length` 或等价值：判定为截断；
- `stop` 但无法解析：判定为语法错误；
- 没有元数据时，只能把未闭合括号、字符串中断等特征作为诊断信号，不能据此自动发布结果。

截断意味着内容可能根本没有生成，适合重新生成当前单元；语法错误通常内容完整，适合一次格式修复。

### 4.2 格式修复只接收当前单元

格式修复 Prompt 输入：

```json
{
  "unit_kind": "act_fragment",
  "expected_top_level_keys": ["beats"],
  "compact_schema": {},
  "raw_text": "...",
  "parse_error": "..."
}
```

要求模型只恢复 JSON 结构，不改写故事事实。响应仍必须经过同一个提取器和后续完整业务校验。

不得为了修复一个 Act，再传入完整 Canon、两份参考故事和所有其它 Act。

### 4.3 无法解析的 StoryPlan 不能强行拆分

只有获得合法顶层对象后，才能安全按区段拆修。如果 StoryPlan 文本本身被截断，代码无法可靠判断缺少哪些数组，也不能通过字符串切片构造有效计划。

因此固定策略是：

- StoryPlan 截断：使用更高输出预算完整重生成一次；
- StoryPlan 完整但语法错误：格式修复一次；
- 已解析 StoryPlan 的业务问题：交给 StoryPlan 区段修复方案；
- 不对半截 JSON 做区段级业务修复。

## 5. 结构化输出能力

如果供应商确认支持 `response_format=json_schema`，可以按模型能力配置启用，但不能把它作为唯一保障。OpenAI 兼容接口不代表所有供应商都完整实现相同结构化输出协议。

建议模型配置增加能力声明：

```python
class ModelCapabilities(BaseModel):
    json_object: bool = False
    json_schema: bool = False
    reports_finish_reason: bool = True
```

调用层按能力绑定参数，解析和业务校验仍必须保留。

## 6. 安全与持久化

- 原始响应只允许在显式 debug 开关下保存到受限诊断目录，并设置 TTL。
- 常规日志只记录字符数、哈希、错误类型、模型、阶段和 token。
- 解析失败不得调用 `on_artifact()`。
- 格式修复产物必须标记 `source_attempt_id`，方便追踪一次生成对应哪次修复。
- 面向用户只返回阶段级错误，例如“故事规划输出不完整，已重试仍失败”。

## 7. 测试要求

1. Markdown 代码围栏内的完整对象能够提取。
2. 空响应分类为 `empty`。
3. `finish_reason=length` 分类为 `truncated`。
4. 截断只触发一次重新生成，不触发业务修复。
5. 完整但多一个逗号的 JSON 只触发一次格式修复。
6. 格式修复后仍要经过 Pydantic 和业务校验。
7. 两次失败后显式结束，不进入无限重试。
8. 生产日志不包含完整原始 StoryPlan 或用户确认稿。

## 8. 验收标准

- JSON 解析失败不再绕过恢复流程。
- 解析重试和业务修复的计数、错误码完全分离。
- 截断不会被代码猜测成合法 JSON。
- 单个分片解析错误不会导致重新生成已经验证的其它分片。
- 每个生成单元的解析恢复最多额外调用模型 1 次。

