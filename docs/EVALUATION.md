# 评测中心

评测中心用固定题目和预期结果检查 Agent 的行为，支持命令行运行和可筛选的 HTML 报告。报告包含分类通过率、失败原因、每一步的预期与实际结果、SQL、执行事件，以及真实执行时的耗时和调用统计。

## 先看离线演示

新增 [200 案例场景基准](../evals/benchmark200/README.md)，包含 150 个单轮查询、30 个多轮任务切换与 20 个跨源审批案例，开发集 120 例、验收集 80 例。该题集采用 `review_policy=always`，与原 30 题交互策略不同，请按其说明运行。参考答案回放只验证评分器，不能作为模型通过率。

在项目根目录执行：

```powershell
.venv\Scripts\data-agent.exe eval validate evals/sample/suite.json
.venv\Scripts\data-agent.exe eval run evals/sample/suite.json --observations evals/sample/demo_observations.json --output outputs/evaluations/demo
Start-Process outputs/evaluations/demo/report.html
```

默认是 `replay` 模式，不调用模型、不连接业务数据库。30 个案例中有 5 个故意构造的错误，应显示 **25/30 通过**，命令退出码为 1。这用于演示失败分析，不代表模型准确率。每次使用新的输出目录，已有报告不会被覆盖。

退出码：0 表示全部案例通过；1 表示至少一个案例失败；2 表示输入、配置或文件有误。

## 运行真实 Agent 评测

先按项目说明配置模型访问凭据，再执行：

```powershell
$env:DATA_AGENT_SAMPLE_MODE = "true"
$env:DATA_AGENT_SAMPLE_DIR = "scripts/data"
.venv\Scripts\data-agent.exe eval run evals/sample/suite.json --mode live --config all --allow-approvals --review-policy cross_source --timeout 120 --output outputs/evaluations/live-001
```

这会调用当前配置的模型并消耗模型额度，业务数据使用本地 SQLite 样例。样例基准涉及全部 6 个数据源，因此使用 `--config all`。运行前会校验样例文件 SHA256，防止数据已变更但仍使用旧标准答案。其他自建数据集可以使用配置名或 YAML 路径。

每个案例创建独立的 Agent、对话 ID 和内存检查点；同一案例的多步输入共享状态。案例依次执行，一个案例异常后仍会继续后续案例。评测通过 `DataAgentFlow.run()` 走实际编排、校验、SQL 执行和汇总链路。

`--timeout` 是每个案例异步执行阶段的时间预算，包含所有步骤。它不能强制终止同步构造或已提交给线程的数据库操作；数据库驱动仍需自己的查询超时。清理阶段另设最多 10 秒的等待预算。

## 人工参与如何评测

评测输入中必须明确写出每一步，不会看到暂停就自行确认：

```json
{
  "id": "review_example",
  "category": "人工确认",
  "steps": [
    {
      "question": "分别统计销售订单数和库存记录数，不做关联。",
      "expect": {"status": "plan_review"}
    },
    {
      "resume": {"action": "approve"},
      "expect": {"status": "success"}
    }
  ]
}
```

包含 `approve` 的真实评测必须传入 `--allow-approvals`，表示允许执行数据集预先写好的确认步骤。执行器只使用当次暂停返回的 `plan_version`，不伪造版本。支持 `approve`、`cancel`、带 `feedback` 的 `revise`，以及用 `question` 回复澄清。

内置案例覆盖暂停、批准、取消、修改后再次审核和需要澄清的状态。多轮案例覆盖员工总数到在职员工数的追问。行为案例中的状态断言只能证明对应状态符合预期；如需证明结果内容正确，应继续增加表格、任务状态或回答断言。

## 标准答案与评分规则

`evals/sample/` 包含：

| 文件 | 用途 |
|---|---|
| `suite.json` | 30 个案例、输入步骤和断言 |
| `reference_sql.json` | 24 个查询结果案例的参考 SQL，供人工审阅 |
| `data_manifest.json` | 样例数据的文件指纹 |
| `demo_observations.json` | 合成演示输出，包含 5 个故意错误 |

参考结果由手写 SQL 在固定本地数据上执行得到，不向待测模型传递参考 SQL。题目规定结果列名，以便检查输出契约。更新数据或题目后，审阅 `scripts/build_eval_samples.py` 中的 SQL，再运行该脚本重新生成标准答案和指纹；不要只更新指纹以绕过校验。

新增案例时，可设置以下断言：

- `status`：必填，支持 `success`、`partial`、`error`、`clarification_needed`、`plan_review`、`cancelled`。
- `agents`：实际参与或计划参与的数据源集合。
- `table`：列名、行值和重复次数；默认忽略行顺序，`ordered: true` 检查顺序。列顺序可不同，名称必须匹配。
- `agent_tables`：按助手名称检查跨源任务证据中的结果表；每个指定助手必须有且只有一个成功证据结果，表格沿用 `table` 的相等与截断规则。
- `task_statuses`：指定任务 ID 的状态。
- `error_contains`、`warning_contains`、`answer_contains`：错误、警告或回答中应包含的文本。

表格评分保留重复行，区分 NULL、0、布尔值和字符串；数字 `1` 与字符串 `"1"` 不相等。数值默认绝对容差 `0.000001`、相对容差 0，可按案例调整。结果被截断、行宽错误、列名重复或行数元信息不一致，都不能判定为相等。无序结果使用一一匹配处理容差和重复值，标准答案最多 200 行。

`partial` 表示返回了协作警告或存在未成功的子任务。网络错误、执行器超时或清理失败属于基础设施失败，即使案例预期 `error` 也不能因此通过。没有设置的断言不参与评分。现在支持显式配置的可选答案 Judge；在 `expect.judge` 中提供参考答案与正确性、依据充分性、完整性阈值，并传 `--judge-deployment` 启用。未启用不调用评分模型。详细配置与边界见 [HARNESS.md](HARNESS.md)。

新增确定性断言 `task_dependencies` 与 `join_keys` 检查任务依赖映射和关联键。live 评测统一经过 AgentHarness，请求/模型/任务超时、预算和字段策略由 `DATA_AGENT_HARNESS_POLICY` 指定；报告保存每轮 Trace 与预算。审批恢复使用新的单轮预算，案例仍受外层总超时控制。

## 报告和版本比较

HTML 可按通过状态、分类和文本筛选，展开案例查看各步差异。JSON 保留结构化数据，适合接入 CI。

```powershell
.venv\Scripts\data-agent.exe eval compare outputs/evaluations/live-001/report.json outputs/evaluations/live-002/report.json
```

比较命令列出由通过变失败的案例及由失败变通过的案例。要求数据集指纹、数据版本、样例文件、运行模式、配置名和审批策略一致；模型或源码可以变化，用于检查改动后的回归。报告记录源码指纹、依赖版本和模型配置，便于定位差异。自建远端数据集需自行维护稳定快照和 `data_version`，文件指纹无法证明远端内容未变。

指标定义：

- **案例通过率**：所有步骤的全部已配置断言通过，才算该案例通过。
- **P50 / P95**：真实执行的案例总耗时分位数，采用最近秩法，包含初始化、恢复和清理；不是单次 SQL 耗时。
- **LLM calls**：回调观察到的模型调用次数，按 run ID 去重。
- **Query steps**：进入 `execute_query` 节点的次数；不是数据库服务器实际执行语句总数。
- **Total tokens**：被测 Agent 模型回调提供的 token 用量之和；任一次用量缺失时显示未知，不补零。可选 Judge 用量单独记录；Harness 显式配置费率与费用预算时提供估算费用，不等同于实际账单。

新增 `data-agent eval gate candidate/report.json evals/harness/live_gate.example.json --baseline baseline/report.json`，支持质量下限、资源上限和基线回归阈值。缺少所需指标时失败，回放不提供运行性能指标。阈值文件是待校准示例，详见 Harness 文档。

回放不报告模型耗时、调用量和 token 用量。执行事件仅记录名称、状态与耗时，最多 1000 条，不保存提示词或模型思维过程。报告仍包含业务结果、SQL 和最终回答，请保存在适合这些数据访问权限的位置。

## 本地验证

```powershell
.venv\Scripts\python.exe -m pytest tests/test_evaluation.py tests/test_human_review.py tests/test_collaboration.py -o addopts='' -p no:cacheprovider -q
```

这些测试使用本地数据、模拟模型和真实编译的 LangGraph，不依赖外部模型服务。后续可以在这套基础上补充失败用例采集、人工标注、定时回归和在线管理界面。
