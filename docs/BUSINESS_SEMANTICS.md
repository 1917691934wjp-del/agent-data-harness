# 业务口径与查询上下文框架

目标：查询不仅能够执行，还应符合用户确认的统计对象、范围、时间和单位。

## 执行路径

```text
用户问题 + 会话上下文
          ↓
SchemaSnapshot：实际可访问表、字段和版本
          ↓
业务指标目录：公式、粒度、别名、单位、时间能力、缺失字段
          ↓
Coordinator：识别指标、提出条件更新、判断新话题或追问
          ↓
口径模糊或数据不足 → 澄清 → 用户补充 → 重新规划
          ↓
prepare_context：校验指标来源、合并条件、保存用户证据
          ↓
review_plan：确认提议、批准 / 修改 / 取消
          ↓
Worker：共享 Schema 和指标目录，按确认口径生成并校验 SQL
          ↓
结果汇总 + 固定数据集评测
```

## 指标定义

`context.py` 中的 `MetricDefinition` 是指标契约。`expression` 是受审阅的口径说明，不是自动执行模板；生成 SQL 仍经过原 SQL 校验与查询链路。

| 字段 | 含义 |
|---|---|
| id / version | 稳定指标 ID 与口径版本 |
| table / columns | 来源表和必需计算字段 |
| grain | 来源数据粒度所需字段 |
| expression / definition | 计算公式与业务边界 |
| aliases | 可识别的常见表达 |
| unit | 计数或数值单位，不能据此猜币种 |
| time_column | 可用于时间筛选的字段；无字段不支持历史筛选 |
| null_policy | 空值处理约定 |
| source | 定义来源，内置值为项目示例口径 |

内置目录包含库存量、库存记录数、低库存记录数、低库存产品数、订单记录数、订单金额、当前在职员工数和可用库存。可用库存缺少 `reserved_quantity` 时标记不可用。订单金额包括全部状态，不自动等同于实收收入。`IsActive` 只能表达当前状态，不能推算历史在职人数。

## 配置自己的业务指标

设置 `DATA_AGENT_METRICS_FILE` 指向 JSON 数组文件。自定义目录整体替换默认目录，应包含需要保留的全部指标。定义必须人工审阅，未知字段和重复 ID 会被拒绝。

```json
[
  {
    "id": "order_records",
    "name": "订单记录数",
    "version": "1",
    "table": "orders",
    "columns": ["order_id"],
    "grain": ["order_id"],
    "expression": "COUNT(*)",
    "definition": "统计全部状态订单记录，不代表销量。",
    "aliases": ["订单数", "订单总数"],
    "unit": "条",
    "time_column": "order_date",
    "null_policy": "计数全部记录。",
    "source": "业务负责人确认的指标字典"
  }
]
```

目录按每个数据源实际 Schema 过滤。规划器和 Worker 都能看到相关指标的 `available`、`missing_columns` 和 `time_filter_supported`；缺字段的指标不能通过 `prepare_context` 的指标选择校验。当前 table 按快照中的表名精确匹配。

## 表达与澄清

| 输入 | 处理要求 |
|---|---|
| 库存不足有多少 | 澄清统计记录数还是去重产品数 |
| 库存不足记录数 | 对应 low_stock_records |
| 订单总金额 | 对应 order_amount，保留全部状态定义 |
| 销售额是多少 | 澄清订单金额、实收收入或净销售额 |
| 可用库存 | 缺占用量字段时解释缺失，不能改查实物库存冒充答案 |
| 去年在职人数 | 缺任职历史时说明不能由当前状态计算 |

别名是识别证据，不做模糊字符串自动确认。`prepare_context` 检查模型引用的用户证据是否出现在当前输入中；未明确表达的条件标记 proposed，通过已有审批机制确认。

## 多轮上下文

沿用现有 `QueryContext`：metric_id、warehouse_scope、time_range。每个条件记录 value、status、evidence、reference_time 和 approved_plan_version。

- followup：深拷贝旧条件，仅覆盖明确变更。
- new：创建新上下文，防止旧仓库或时间条件污染新问题。
- 清除条件：必须引用“全部仓库”“不限时间”等与字段对应的明确表达。
- Schema 或指标版本变化：已审批条件重新标记 proposed。
- Worker：仅收到所属指标；非库存 Worker 不接收仓库条件。

当前每轮只有一个主指标上下文，多任务问题可以返回各源证据，但多个指标独立的长期上下文尚未建模。时间条件仍是文字表达，不包含确定性日期解析器。

## 验证与边界

运行 `tests/test_business_metrics.py`、`tests/test_context.py`、`tests/test_collaboration.py` 和 `tests/test_human_review.py`，验证缺字段拒绝、目录替换、时间能力、跨源隔离、继承、重置和审批恢复。

Schema 字段可用性、指标选择、条件来源和审批有代码检查。自然语言歧义识别、话题切换以及 SQL 是否完全符合指标含义，仍依赖模型与评测；现阶段没有通用 SQL 语义等价证明。不得将“SQL 执行成功”作为业务正确性的证据。

后续增加真实评测案例：同义表达、含糊计数、否定条件、跨话题切换、缺字段、历史在职人数，以及订单金额与收入混淆。通过固定数据、参考 SQL 和结果断言比较改动前后效果。
