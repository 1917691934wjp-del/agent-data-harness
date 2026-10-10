# 多数据源 Agent 200 案例基准

这是一套固定本地业务样例数据上的场景基准，不是公开 Spider/BIRD 成绩，也不是 200 个独立 SQL 问题。题目包含 150 条不同的参考查询，另外 50 个案例检验同域多轮任务切换和跨源审批流程，复用其中部分查询。

| 类型 | 案例 | 评分依据 |
|---|---:|---|
| 单轮查询 | 150 | 计划状态、路由、执行结果列名与行值 |
| 同域多轮任务切换 | 30 | 同一会话内两次查询的计划、路由和结果 |
| 跨源审批交互 | 20 | 暂停、取消、批准、修改后批准；执行时核对各源结果 |
| 合计 | 200 | 460 个显式交互步骤 |

六个助手分别有 25 个单轮案例和 5 个多轮案例。单轮问题覆盖基础统计、条件与边界、分组与指标、排序与时间、关联与子查询。产品目录单表案例采用子查询和 JSON 数组长度，不伪造不存在的关联表；参考方言为本地 SQLite，不用于直接衡量远端 Cosmos 或其他数据库方言能力。

## 文件与划分

- `suite.json`：完整 200 案例。
- `development.json`：120 个开发案例，适合调试提示词、流程和资源预算。
- `acceptance.json`：80 个验收案例，建议在固定候选版本后运行。
- `smoke.json`：10 个开发集案例，覆盖六个助手、多轮、跨源批准、修改和取消。
- `reference_sql.json`：参考 SQL、来源文件、对应步骤、助手及执行得到的结果表；不传给待测模型。
- `data_manifest.json`：六份数据文件 SHA256。
- `coverage.csv`：每例的领域、题目家族、划分与参考查询数量。
- `audit.json`：案例数、步骤数、独立查询数、跨集重复检查及审核状态。
- `oracle_replay.json`：由参考答案构造的正向评分器验证，预期 200/200。
- `negative_replay.json`：含 8 个故意错误的评分器验证，预期 192/200，退出码 1。

按题目家族分组，关联的单轮、多轮和审批案例保持在同一划分。开发集与验收集案例 ID、参考 SQL 均无重复。两个划分难度分布不同：开发侧重基础、条件、分组，验收侧重排序、时间、关联和子查询；应分别报告分类成绩，不按比例直接比较其难度。

这些划分用于项目内验收，不是对模型训练数据的盲测保证，且可能与既有 30 题或系统 few-shot 示例共享基础题型。参考 SQL 已构造并逐条执行验证，仍建议由独立人员审核业务口径；不标称人工标注金标准。

## 固定交互策略

**必须使用 `--review-policy always --allow-approvals`。** 每个执行查询的案例明确写出“提问 → 计划审阅 → approve → 结果检查”。暂停和取消案例不额外审批；修改案例明确写出 revise 后再 approve。执行器使用真实返回的计划版本，不自动补审批、不绕过人工确认。

这与旧 30 题的 `cross_source` 策略不同，不能直接把新旧通过率作提升对比。新基准耗时包含用户确认的模拟恢复步骤。多轮案例明确重述目标，测试任务切换和结果恢复，不等同于省略指代或长期记忆测试。

跨源批准案例用新增 `expect.agent_tables` 按助手核对证据表，必须恰好有一个对应助手的成功结果；缺失、重复、失败、截断或数值错误均失败。不同源的表格不会被强行合并成一个结果表。

## 运行方式

```powershell
.venv\Scripts\data-agent.exe eval validate evals/benchmark200/suite.json

# 评分器正向验证，不调用模型
.venv\Scripts\data-agent.exe eval run evals/benchmark200/suite.json --observations evals/benchmark200/oracle_replay.json --output outputs/evaluations/benchmark200-oracle-new

# 评分器负向验证，应有 8 例失败，退出码 1
.venv\Scripts\data-agent.exe eval run evals/benchmark200/suite.json --observations evals/benchmark200/negative_replay.json --output outputs/evaluations/benchmark200-negative-new

$env:DATA_AGENT_SAMPLE_MODE = "true"
$env:DATA_AGENT_SAMPLE_DIR = "scripts/data"

# 真实模型抽测 10 个开发案例
.venv\Scripts\data-agent.exe eval run evals/benchmark200/smoke.json --mode live --config all --allow-approvals --review-policy always --timeout 120 --output outputs/evaluations/benchmark200-smoke-new

# 真实模型全量 200 案例，消耗 API 额度
.venv\Scripts\data-agent.exe eval run evals/benchmark200/suite.json --mode live --config all --allow-approvals --review-policy always --timeout 120 --output outputs/evaluations/benchmark200-live-new

# 候选版本固定后单独运行 80 例验收集
.venv\Scripts\data-agent.exe eval run evals/benchmark200/acceptance.json --mode live --config all --allow-approvals --review-policy always --timeout 120 --output outputs/evaluations/benchmark200-acceptance-new
```

每次换新输出目录，报告不覆盖。全量默认串行运行，不保证完成时间或预设模型成绩。重复三轮可以衡量稳定性，但仍只有 200 个案例，不是 600 个独立题目。

## 重新生成与维护

```powershell
.venv\Scripts\python.exe scripts/build_benchmark_200.py
.venv\Scripts\python.exe -m pytest tests/test_benchmark200.py -o addopts='' -p no:cacheprovider -q
```

生成器维护显式中文题目与 SQL，不通过批量同义改写凑数；运行前须审核 SQL 和题意变更。更新数据、标准答案或交互策略后重新建立版本基线，不根据待测模型输出改标准答案以提高分数。

当前不包含提示词注入、跨用户越权、故障注入、线上负载和自然语言答案 Judge 专项。账号安全使用独立回归测试集，不能混算为模型能力通过率。
