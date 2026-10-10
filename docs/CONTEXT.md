# Schema、库存指标与用户条件

默认多智能体流程现在使用共享 schema 快照、内置库存指标字典和检查点中的结构化查询条件。模型调用前的上下文能跨规划、SQL 生成、修复和汇总保持一致。

## 统一 schema 来源

`context.py` 中的 `build_schema_snapshot()` 在数据源初始化时构建快照。SQL 数据源通过 SQLAlchemy 读取当前连接可见的表、字段、类型、可空性、主键和外键，不读取业务样本行。配置中的描述、枚举和约束作为业务注释叠加，实际字段类型以数据库为准。配置引用不存在的表或字段时明确报错，避免继续使用过期 schema。

协调器目录和对应 worker 使用同一份 `SchemaSnapshot`。本地样例模式不再出现 worker 能看到真实字段、协调器却只有数据源描述的情况。快照包含内容指纹；更新数据库结构或配置后需重新创建 flow，当前版本不在每个请求前重新扫描数据库。

Cosmos 等非 SQL 数据源使用配置声明的 schema，并标记 `declared (not database-verified)`。声明结构不能视为已验证的数据库约束。数据库访问权限仍由连接账号控制，这个目录不替代权限校验。

## 库存指标字典

`context.py` 的 `INVENTORY_METRICS` 维护以下项目默认定义，仅当 inventory 表包含所需字段时才向模型提供：

| ID | 名称 | 口径 |
|---|---|---|
| `inventory_quantity` | 库存数量合计 | 对选定范围求 `SUM(quantity)`；不是扣除占用量后的可用库存 |
| `inventory_records` | 库存记录数 | `COUNT(*)`；不是去重产品数 |
| `low_stock_records` | 库存不足记录数 | 逐条判断 `quantity < reorder_level` 后计数 |
| `low_stock_products` | 库存不足产品数 | 在选定仓库范围内，任一记录不足的去重产品数 |

每项记录版本、所需字段、粒度、表达式、业务解释和别名。默认粒度为产品与仓库，但不承诺该组合在原始数据中唯一。NULL 不自动补零；最后一个指标不代表跨仓汇总后仍然不足。

这些是明确声明的项目默认口径，不是系统从字段名推断出的企业业务事实。实际业务定义变化时，应修改定义并更新版本。字典给模型提供生成约束，不直接拼接执行 SQL，也不证明生成 SQL 一定符合业务口径；结果仍需校验和评测。

## 结构化保存用户条件

计划增加 `context_mode` 和 `context_updates`。目前支持三个字段：`metric_id`、`warehouse_scope`、`time_range`。

- `new`：新主题，清空上一查询的有效条件。
- `followup`：追问，保留未变字段，只应用本次更新。
- 更新包含值及用户原话 `evidence`，模型不能写入审批状态。
- 清除字段需给出相应原话，例如“所有仓库”“不限时间”，不能用“所有仓库”同时删除时间限制。

运行时区分三种状态：

| 状态 | 来源与行为 |
|---|---|
| `user_stated` | 在当前用户输入中找到对应原话；属于用户表达，不等同于已审批 |
| `proposed` | 无法核对原话或包含否定等歧义；必须经过人工审批 |
| `approved` | 用户通过当前计划版本的审批，保存 `approved_plan_version` |

原话核对采用保守的字符串规则，不是完整的语义证明；同义词归一化可能多触发一次审批。新话题还是追问的判断仍由规划模型完成，应纳入真实评测。

条件同时保存原话和带时区的 `reference_time`，供解释“上个月”等相对时间。此版本不自行将自然语言时间编译成强制 SQL 日期范围，也不自动将仓库名称映射成 ID。

检查点中维护三个对象：

- `query_context`：当前计划的候选或已批准条件，包含完整指标定义、指标所属 agent 和 schema 指纹。
- `conversation_context`：最近一次进入执行阶段的条件，供后续追问使用；其存在不意味着查询成功。
- `confirmed_context`：最近一次通过显式审批的快照，作为确认记录。新主题不会自动沿用它，取消或未确认的修改不会覆盖它。

执行结果和评测报告暴露 `query_context` 与 `confirmed_context`。库存专属条件只发给相关 worker，通用时间条件可传给其他参与任务。跨线程不共享这些对象。

## 与人工审批的关系

审批消息展示指标、仓库范围、时间范围、口径版本和粒度。计划修改会重新规划并产生新 `plan_version`；旧版本确认继续被拒绝。

即使单数据源查询在默认策略下无需审核，只要条件包含 `proposed`，也会暂停要求确认。`review_policy=never` 时不弹出审核，但也不执行未确认的推测，返回 `unconfirmed_context`，要求用户补充条件。

恢复审批时会比较当前 flow 的 schema 和指标版本。若在重建 flow 后发生变化，旧审批会触发重新规划与再次确认。审批只说明用户同意该计划，并不替代 schema、SQL 或业务结果校验。

默认内存 checkpointer 可在同一进程内保留条件。需要跨重启保存时，使用已有 SQLite 历史功能或注入持久化 checkpointer；服务端重启后可从待审核计划继续，不必重复生成原计划。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest tests/test_context.py tests/test_collaboration.py tests/test_human_review.py tests/test_evaluation.py tests/test_sample_data.py tests/test_history.py -o addopts='' -p no:cacheprovider -q
```

测试覆盖真实 SQLite schema、无效配置注释、库存参考 SQL、生成与修复上下文一致、长对话保留、澄清回复、批准与取消、跨线程隔离、持久化恢复及版本变更。本地测试使用模拟模型，没有调用收费模型；模型提取条件的实际准确率需要另外运行真实评测。

旧的 `enable_collaboration=False` 路径仍可使用统一 schema 和库存字典；结构化条件提取与审批合并由默认多智能体协调器提供。
