# 本地示例数据

在项目根目录的 `.env` 中设置：

```dotenv
DATA_AGENT_SAMPLE_MODE=true
DATA_AGENT_SAMPLE_DIR=scripts/data
```

重启应用并新建聊天后，原有业务入口及 All Agents 会使用对应 JSON 文件。
每个数据源加载到独立的内存 SQLite 数据库，避免不同业务的同名表冲突。
不需要安装数据库服务或配置云数据库凭据；JSON 文件不会被修改。
重建连接时重新读取文件，查询连接为只读。

| 文件 | 表数 | 记录数 |
| --- | ---: | ---: |
| azure_sql_data.json | 4 | 75 |
| bigquery_data.json | 5 | 70 |
| cosmos_data.json | 1 | 45 |
| databricks_data.json | 2 | 258 |
| postgres_data.json | 3 | 195 |
| synapse_data.json | 6 | 77 |
| 合计 | 21 | 720 |

查询使用 SQLite 语法。表结构从文件实际字段生成；嵌套对象和数组保存为
JSON TEXT，可用 `json_extract`、`json_each` 查询。库存文件中省略的自增主键
按照记录顺序补齐，以支持仓库与库存、发货记录关联。

本地模式会替换云数据库的 SQL 提示词、静态表结构和示例查询。
自然语言问答仍需要有效的模型凭据；本地数据模式只替换数据库连接。
设为 `DATA_AGENT_SAMPLE_MODE=false` 并重启应用即可恢复原有数据库配置。
