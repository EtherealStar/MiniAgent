# calculator 工具设计

本文定义内置 `calculator` 的稳定契约。它服从 [`tool-design-guidelines.md`](../tool-design-guidelines.md) 与 [`tool-registry-and-execution.md`](../tool-registry-and-execution.md)。

## 1. 目的与边界

`calculator` 是受限、确定性的纯数值表达式求值器。它负责可靠计算，不负责解释自然语言数学题，也不是符号代数系统。

工具不支持变量赋值、方程求解、微积分、单位或货币换算、随机数、复数、数组、属性访问、索引、导入或任意 Python 调用。它不使用 `eval()`，不访问文件、网络、Session 或环境变量。

Provider-visible description 固定为：

```text
Evaluate a restricted numeric expression with configurable decimal precision.
```

## 2. ToolInput

```python
class CalculatorInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=False,
    )

    expression: str
    precision: int = Field(default=15, ge=1, le=100)
```

`expression` trim 后不得为空，最长 1024 个 Unicode 字符。`precision` 表示非整数计算和最终实数格式化使用的十进制有效数字；默认 15，范围 1..100。一次 ToolUse 只计算一个表达式。

## 3. 表达式语言

表达式使用受限的 Python 风格语法，仅允许：

- 十进制整数、小数和科学计数法字面量；
- 括号；
- 二元运算 `+`、`-`、`*`、`/`、`//`、`%`、`**`；
- 一元 `+`、`-`；
- 白名单名称和直接函数调用。

不允许 bool、字符串、bytes、容器、比较、布尔运算、条件表达式、lambda、推导式、属性、下标、关键字参数或其他 AST 节点。解析器从源 token 文本构造十进制数，不能先经过 Python float 而引入二进制误差。

白名单函数：

```text
abs, sqrt, exp
ln, log, log10, log2
sin, cos, tan
asin, acos, atan, atan2
sinh, cosh, tanh
floor, ceil, round
min, max
degrees, radians
```

白名单常量：

```text
pi, e, tau
```

名称区分大小写且不接受 `math.`、`mp.` 等模块前缀。三角函数使用弧度；`degrees` 和 `radians` 进行显式转换。`log(x)` 与 `ln(x)` 是自然对数，`log(x, base)` 使用显式底数。

`a // b` 定义为 `floor(a / b)`，`a % b` 定义为 `a - b * floor(a / b)`，包括负数时也保持这一关系。`round(x)` 返回整数；`round(x, ndigits)` 使用十进制位数和 ties-to-even 规则。函数 arity 必须严格校验，`min` 和 `max` 最多接受 32 个参数。

## 4. 数值引擎与精度

实数计算使用 `mpmath`。每次调用必须克隆独立 mpmath context，在 `precision` 基础上增加固定 guard digits；不能修改共享 `mpmath.mp.dps`，否则并发 Calculator ToolUse 会互相污染。

整数输入和只含整数闭合运算的中间值尽可能使用 Python arbitrary-precision int 保持精确。`/`、非整数输入、常量和超越函数提升为独立 mpmath context 的实数。负底数的非整数幂、定义域外函数以及任何复数结果都失败，不自动转为复数。

复杂度预算：

- AST 最多 128 个节点；
- AST 嵌套深度最多 32；
- 单个数值字面量最多 256 位；
- 单次调用最多 32 个参数；
- 精确整数中间值最多 4096 bits；
- 幂指数绝对值最多 10,000；
- 最终值必须是有限整数或有限实数。

超过预算不生成巨大 artifact；它是预期执行失败。

## 5. ToolTarget、执行与失败

resolver 显式返回空 targets。classifier 固定返回 `concurrency_safe=True`。同步求值放入 `asyncio.to_thread()`；ToolSpec timeout 为 5 秒，RetryPolicy 为单次 attempt，ResultPolicy 使用系统默认值。

复杂度限制与业务失败使用框架级 ExecutionErrorCode：

- 除零、定义域错误、复数或非有限结果：`DOMAIN_ERROR`；
- AST、整数或其他资源预算超限：`RESOURCE_EXHAUSTED`；
- 合法表达式的其他预期求值失败：`OPERATION_FAILED`。

表达式语法、未知名称、非法 AST、函数 arity 和 schema 错误在验证阶段产生 `invalid_arguments`。handler safe_message 可具体说明 `Division by zero.` 或 `The result is outside the real-number domain.`，但不创建工具私有顶层 code。calculator 不声明 transient retry。

## 6. ToolOutput

```python
class CalculatorMetadata(BaseModel):
    precision: int
    exact: bool

class CalculatorData(BaseModel):
    kind: Literal["integer", "real"]
    value: str

class CalculatorOutput(ToolOutput):
    metadata: CalculatorMetadata
    data: CalculatorData
```

`content` 必须等于 `data.value`。精确整数使用完整十进制文本；实数按请求有效数字在普通或科学计数法之间稳定选择，移除无意义尾随零并把负零规范化为 `0`。`data.value` 必须是字符串，不能使用 JSON number 重新引入精度损失。

metadata/data 不保存原始 expression、AST、中间值或运行时 context。`exact=true` 只表示最终值由精确整数语义得到。

## 7. Prompt

```python
PROMPT = """Purpose:
Evaluate a numeric expression reliably with controlled decimal precision.

Use when:
- You need arithmetic, powers, logarithms, trigonometry, rounding, or a numeric constant evaluated.

Prefer instead:
- Reason directly when the task is symbolic, requires a proof, solves an equation, or involves units or live exchange rates.

Rules:
- Provide a numeric expression, not a natural-language word problem.
- Trigonometric functions use radians unless you convert explicitly with `degrees` or `radians`.
- Only the documented operators, functions, and constants are available.

Returns:
- One finite integer or real value as precision-safe text.

If it fails:
- Simplify an expression that exceeds the complexity budget or correct a syntax or mathematical domain error.
"""
```

## 8. 验收不变量

- input schema 只有 `expression` 和 `precision`，严格拒绝多余字段与宽松类型；
- `0.1 + 0.2` 不产生二进制浮点噪声，精确整数不会因 precision 降低正确性；
- AST 白名单拒绝属性、索引、导入、赋值、容器、比较和任意调用；
- 函数、常量、arity、弧度、log、floor division、modulo 和 round 语义稳定；
- 每次调用使用独立 mpmath context，并发不同 precision 不互相污染；
- 节点、深度、字面量、参数、整数和指数预算均在执行前或执行中受控；
- 复数、NaN、无穷和定义域外结果按通用 ExecutionErrorCode 失败；
- output 只保存字符串值和必要执行事实，不回显 ToolInput 或中间数据。
