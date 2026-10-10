# Agent Runtime & Evaluation Harness

## 体系与入口

统一入口已接入 CLI、Chainlit UI、A2A 请求/取消和 live evaluation。复用现有 DataAgentFlow、LangGraph、SQLValidator、协作调度、检查点、人工审批和确定性评分。

```text
CLI / UI / A2A / Live Eval
          ↓
AgentHarness → 输入约束 / 请求预算 / 独立观察器
          ↓
DataAgentFlow → 规划 / 澄清 / 审批 / 恢复
          ↓
Coordinator → 任务数量 / 数据源允许列表 / 并发 / 任务超时
          ↓
Worker → 模型调用预算 / 模型超时 / SQL 校验 / 字段策略
          ↓
DB / CodeExecutor → 工具预算 / 行数限制 / 代码环境限制
          ↓
HarnessResult → Trace / 用量 / 耗时 / 状态

EvalSuite → 同一 Harness → 确定性评分 / 可选答案 Judge
          ↓
JSON + HTML → Baseline Compare → Quality Gate → CI 退出码
```

## 配置与使用

```python
from data_agent.agent import DataAgentFlow
from data_agent.harness import AgentHarness, RuntimePolicy

async with DataAgentFlow() as flow:
    harness = AgentHarness(flow, RuntimePolicy(request_timeout=120))
    result = await harness.run("统计库存记录数")
    print(result.status, result.metrics, result.budget)
    # 恢复审批使用 result.thread_id 和原暂停输出中的 plan_version。
```

兼容入口 run_output() 返回原输出并附加 harness 元信息；请求超时和预算超额分别抛出 TimeoutError、BudgetExceeded，让现有 UI/API 错误处理接管。直接调用 flow.run() 保持可用，但只有经过 Harness 的请求才执行统一策略。

```powershell
$env:DATA_AGENT_HARNESS_POLICY = "evals/harness/runtime_policy.json"
$env:DATA_AGENT_HARNESS_TRACE_DIR = "outputs/harness-traces"
```

策略 JSON 严格拒绝未知字段，未指定文件使用 RuntimePolicy 默认值。默认不允许 LocalExecutor 执行模型生成代码；开发环境本机执行需显式设置 allow_local_code=true。生产使用项目已有 Azure Sessions，并提供原有端点配置。

| 参数 | 默认值 | 执行位置 |
|---|---:|---|
| request_timeout | 60 秒 | 单轮 flow.run 异步总预算 |
| model_timeout | 30 秒 | 规划、SQL、修复、回答、可视化模型调用 |
| task_timeout | 30 秒 | 单 worker，取与已有设置较小值 |
| max_llm_calls | 20 | 模型启动回调前检查，按 run ID 去重 |
| max_tool_calls | 20 | SQL/代码执行边界的尝试次数 |
| max_retries | 6 | 全请求 SQL 修复节点预算 |
| max_agent_tasks | 8 | 协作任务数，旧单助手路径按一个任务统计 |
| max_concurrency | 4 | 调度批次，取与已有设置较小值 |
| max_result_rows | 10000 | SQL LIMIT 和返回结果大小 |
| max_question_chars | 12000 | 问题、恢复问题和修改意见长度 |
| max_trace_spans | 1000 | 节点/模型 Span 上限，超额标记截断 |

max_tokens 默认未设置。设置后在模型返回用量时结算，超额阻止后续调用。max_cost_usd 还要求显式设置 input_usd_per_million 与 output_usd_per_million，是输入/输出 token 估算费用，不是实际账单。多模型请求使用能覆盖部署单价的费率；缓存折扣、供应商隐式重试与其他收费不单独计算。无法获得用量时，已配置 token/费用预算会拒绝继续执行；未配置时保持未知。

Token/费用不是预付保留额度，最后一次调用和已开始的并发调用可能超额。模型供应商内部网络重试不视为独立 Agent 调用。

## 状态、恢复与取消

completed 表示流程完成，业务正确性由评测负责。其他状态有 paused、partial、error、cancelled、timeout、budget_exceeded。参数错误、意外异常向调用者传播，外部取消保留 asyncio.CancelledError。

HITL 暂停结束当前轮，恢复创建新 run ID 和预算，保留 thread ID 与检查点。审批版本、取消、修改和 schema/口径变化后的重新审批由原 Flow 负责，Harness 不自动批准或重试整轮。

预算不包含 Flow 构造、连接与清理。异步取消不能强制停止同步驱动查询或线程中的代码，必须另配数据库驱动查询超时。任务超时可能返回部分失败，模型超时在预算元信息中记录。远程服务端真正取消仍取决于供应商。

## Trace 与统计

每轮独立 collector，不修改 Flow 共享回调列表。节点与模型 Span 有 span_id、parent_span_id、名称、时间、耗时与状态，模型名和输入/输出 token 在可获取时记录。请求记录含总耗时、LLM 调用、查询步骤、工具/修复/任务预算和超额原因。

设置 TRACE_DIR 后用 UUID 文件名独占写入 metadata-only JSON，覆盖完成、异常、取消与超时，不写问题、提示词、SQL、业务结果或异常消息。存储权限失败不会重复业务执行，正常返回结果中标记 trace_export_error。原输出、评测报告和现有应用日志仍可能含业务数据，需按相应权限存放。

## 安全策略

- allowed_agents：明确允许执行的数据源；空列表拒绝全部，未设置沿用注册数据源。
- forbidden_sql_columns：按源列名检查 SQL AST，别名不能绕过；配置后拒绝 SELECT 通配符，COUNT(*) 可用。未支持的 Cosmos 字段 AST 限制采用拒绝执行。
- redact_columns：按返回列名脱敏，在查询结果消息、回答和可视化模型上下文之前处理。
- max_result_rows：约束 SQL LIMIT 并标记截断；截断数据不能通过完整表格相等断言。
- allow_local_code=false：阻止 LocalExecutor 执行；本机 exec 不作为沙箱。

这些是显式权限与数据策略，不宣称通用提示词注入检测或自动识别所有 PII。返回列名脱敏不能阻止重命名、自由文本或间接推断泄露；真正禁止读取的字段还应使用 SQL 字段限制和数据库账号权限。现有 SQLValidator 的只读/危险函数检查继续生效。

## 评分与可选 Judge

保留原状态、路由、表格、任务状态、错误和回答文本断言。新增 task_dependencies 检查完整依赖映射，join_keys 检查关联键。

可选答案评分写入每步 expect.judge：

```json
{
  "status": "success",
  "judge": {
    "reference_answer": "统计口径为记录数，总计 10 条。",
    "min_correctness": 0.8,
    "min_groundedness": 0.9,
    "min_completeness": 0.8
  }
}
```

运行显式传 --judge-provider deepseek --judge-deployment deepseek-flash；Azure 模型可使用 --judge-provider azure_openai --judge-deployment YOUR_AZURE_DEPLOYMENT。未启用不调用 Judge；有 judge 断言却未配置会报错，避免跳过评分。Judge 将问题、参考答案、回答和表格作为不可信数据，不执行工具；返回严格 0~1 分数、短理由与独立调用统计。证据超过预算拒绝评分，不静默截断。耗时包含在案例总时限内，调用与用量单独记录，不混入被测 Agent 调用数。

Judge 可用于明确授权的 live 或 replay 评分；回放调用 Judge 不代表重新测量 Agent 模型能力。LLM 分数存在偏差，应人工抽查并稳定 rubric。

## 回归门禁与报告

HTML 报告包含分类筛选、案例差异、执行事件、质量/运行指标、Harness Trace/预算与可选答案评分，JSON 供 CI 使用。

```powershell
.venv\Scripts\data-agent.exe eval run evals/sample/suite.json --observations evals/sample/demo_observations.json --output outputs/harness-demo-new
.venv\Scripts\data-agent.exe eval gate outputs/harness-demo-new/report.json evals/harness/replay_gate.json
.venv\Scripts\data-agent.exe eval compare outputs/evaluations/baseline/report.json outputs/evaluations/candidate/report.json
.venv\Scripts\data-agent.exe eval gate outputs/evaluations/candidate/report.json evals/harness/live_gate.example.json --baseline outputs/evaluations/baseline/report.json
```

门禁支持案例通过率、路由/结果/规划准确率、答案 Judge 各维平均分、P95、平均 LLM 调用/Token 和案例超时率。准确率仅统计配置对应断言的步骤。缺指标或基线门禁失败，回放不提供运行性能指标，Judge 评分不完整不报告平均分。

min/max 是绝对阈值，max_drop/max_increase 是同单位绝对变化，例如准确率 max_drop=0.02 表示 2 个百分点。比较要求数据集、快照、配置、审批策略、Harness 策略和 Judge 配置一致，模型/源码可变化。退出码：0 通过，1 未通过，2 输入无效。

.github/workflows/harness.yaml 配置离线测试和合成回放门禁，无模型凭据。合成回放故意有 5 个错误，预期 25/30，是评分演示，不是模型准确率。live_gate.example.json 的阈值需要实际校准，不代表已达成性能。

## 验证边界

```powershell
.venv\Scripts\python.exe -m pytest tests/test_harness.py tests/test_harness_system.py tests/test_human_review.py tests/test_evaluation.py tests/test_collaboration.py tests/test_context.py tests/test_history.py tests/test_sample_data.py -o addopts='' -p no:cacheprovider --basetemp outputs/harness-tests-unique-run -q
```

每次使用新 basetemp。离线测试覆盖真实 LangGraph 审批、LangChain 假模型回调、并发隔离、预算阻断、字段策略、取消/超时、评分和报告。没有运行付费 live/Judge、连接远端业务数据库、执行云端 CI 或做生产压测。外围原模块仍有既有 Ruff 告警；Harness、扩展评测和新增测试单独检查。

本次完整离线回归已通过 133 个测试，新增模块与扩展评测通过 Ruff。合成回放按预期为 25/30，replay quality gate 通过。代码中新增默认异步模型调用；自定义 ResponseNode 调用者需 await generate_response，注入的模拟模型需支持 ainvoke。

简历表述见 [HARNESS_RESUME.md](HARNESS_RESUME.md)。
