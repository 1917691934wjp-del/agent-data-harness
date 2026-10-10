"""Rebuild explicit gold SQL cases and a labelled synthetic replay from local data."""

import copy
import hashlib
import json
from pathlib import Path

from data_agent.adapters.sample import create_sample_database

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "sample"
OUT.mkdir(parents=True, exist_ok=True)
queries = [
    ("sales_count", "contoso_sales", "databricks", "统计订单总数", "SELECT count(*) AS total FROM orders"),
    ("sales_customers", "contoso_sales", "databricks", "统计销售客户总数", "SELECT count(*) AS total FROM customers"),
    ("sales_amount", "contoso_sales", "databricks", "统计所有订单 total_amount 总和，不过滤订单状态", "SELECT sum(total_amount) AS total FROM orders"),
    ("sales_status", "contoso_sales", "databricks", "按订单状态统计订单数", "SELECT status, count(*) AS total FROM orders GROUP BY status"),
    ("sales_region", "contoso_sales", "databricks", "按客户所在区域统计客户数", "SELECT region, count(*) AS total FROM customers GROUP BY region"),
    ("sales_empty", "contoso_sales", "databricks", "查询编号为 NOT-EXIST 的订单，返回 order_id", "SELECT order_id FROM orders WHERE order_id='NOT-EXIST'"),
    ("product_count", "contoso_products", "cosmos", "统计产品目录中的产品总数", "SELECT count(*) AS total FROM products"),
    ("product_active", "contoso_products", "cosmos", "统计产品目录中 is_active 为真的产品数量", "SELECT count(*) AS total FROM products WHERE is_active=1"),
    ("product_price", "contoso_products", "cosmos", "计算产品目录中的最高产品价格", "SELECT max(price) AS total FROM products"),
    ("product_category", "contoso_products", "cosmos", "按产品目录的 category 统计产品数", "SELECT category, count(*) AS total FROM products GROUP BY category"),
    ("inventory_count", "contoso_inventory", "postgres", "统计库存记录总数，不是产品去重数", "SELECT count(*) AS total FROM inventory"),
    ("inventory_quantity", "contoso_inventory", "postgres", "统计所有仓库当前库存 quantity 的总和", "SELECT sum(quantity) AS total FROM inventory"),
    ("inventory_warehouses", "contoso_inventory", "postgres", "统计仓库总数", "SELECT count(*) AS total FROM warehouses"),
    ("inventory_low", "contoso_inventory", "postgres", "统计 quantity 小于 reorder_level 的库存记录数", "SELECT count(*) AS total FROM inventory WHERE quantity < reorder_level"),
    ("hr_count", "contoso_hr", "azure_sql", "统计员工总数", "SELECT count(*) AS total FROM employees"),
    ("hr_active", "contoso_hr", "azure_sql", "统计 IsActive 为真的员工数量", "SELECT count(*) AS total FROM employees WHERE IsActive=1"),
    ("hr_salary", "contoso_hr", "azure_sql", "计算所有员工 Salary 的平均值", "SELECT avg(Salary) AS total FROM employees"),
    ("hr_departments", "contoso_hr", "azure_sql", "统计部门总数", "SELECT count(*) AS total FROM departments"),
    ("finance_accounts", "financial_transactions", "bigquery", "统计金融账户总数", "SELECT count(*) AS total FROM accounts"),
    ("finance_transactions", "financial_transactions", "bigquery", "统计金融交易记录总数", "SELECT count(*) AS total FROM transactions"),
    ("finance_alerts", "financial_transactions", "bigquery", "统计欺诈告警记录总数", "SELECT count(*) AS total FROM fraud_alerts"),
    ("finance_null", "financial_transactions", "bigquery", "统计 credit_limit 为空的金融账户数量", "SELECT count(*) AS total FROM accounts WHERE credit_limit IS NULL"),
    ("hotel_count", "hotel_analytics", "synapse", "统计酒店总数", "SELECT count(*) AS total FROM dim_hotel"),
    ("hotel_cancelled", "hotel_analytics", "synapse", "统计 IsCancelled 为真的酒店预订记录数", "SELECT count(*) AS total FROM fact_reservations WHERE IsCancelled=1"),
]
cases, observations, oracle = [], {}, {}
databases = {}
for case_id, agent, source, question, sql in queries:
    db = databases.setdefault(source, None)
    if db is None:
        databases[source] = db = create_sample_database(str(ROOT / "scripts/data" / f"{source}_data.json"))
    with db._engine.connect() as conn:
        cursor = conn.exec_driver_sql(sql)
        columns, rows = list(cursor.keys()), [list(row) for row in cursor]
    table = {"columns": columns, "rows": rows}
    expected = {"status": "success", "agents": [agent], "table": table}
    question += "。结果列名使用 " + ", ".join(columns) + "。"
    cases.append({"id": case_id, "category": "结果正确性", "steps": [{"question": question, "expect": expected}]})
    observations[case_id] = [{"status": "success", "agents": [agent], "table": {**copy.deepcopy(table), "row_count": len(rows)}, "sql": sql}]
    oracle[case_id] = {"source": f"{source}_data.json", "sql": sql}

def case(name, category, steps, outputs):
    cases.append({"id": name, "category": category, "steps": steps})
    observations[name] = outputs

case("followup", "多轮对话", [
    {"question": "统计员工总数，列名 total", "expect": cases[14]["steps"][0]["expect"]},
    {"question": "其中在职的有多少，按 IsActive 为真统计，列名 total", "expect": cases[15]["steps"][0]["expect"]},
], [copy.deepcopy(observations["hr_count"][0]), copy.deepcopy(observations["hr_active"][0])])
review = {"status": "plan_review", "agents": ["contoso_sales", "contoso_inventory"]}
question = "分别统计销售订单数和库存记录数，分开报告两个数据源的结果，不做关联。"
case("review_pause", "人工确认", [{"question": question, "expect": review}], [copy.deepcopy(review)])
case("review_cancel", "人工确认", [
    {"question": question, "expect": review},
    {"resume": {"action": "cancel"}, "expect": {"status": "cancelled", "error_contains": "cancelled"}},
], [copy.deepcopy(review), {"status": "cancelled", "error": "cancelled"}])
case("review_approve", "人工确认", [
    {"question": question, "expect": review},
    {"resume": {"action": "approve"}, "expect": {"status": "success", "agents": review["agents"]}},
], [copy.deepcopy(review), {"status": "success", "agents": review["agents"]}])
case("review_revise", "人工确认", [
    {"question": question, "expect": review},
    {"resume": {"action": "revise", "feedback": "只查询库存记录数"}, "expect": {"status": "plan_review", "agents": ["contoso_inventory"]}},
], [copy.deepcopy(review), {"status": "plan_review", "agents": ["contoso_inventory"]}])
case("clarification", "澄清", [{"question": "帮我查一下那个指标，需要先向我确认指标名称和业务范围。", "expect": {"status": "clarification_needed"}}], [{"status": "clarification_needed"}])

# Deliberate counterexamples make the demo useful for inspecting failures.
observations["sales_count"][0]["table"]["rows"] = [[999]]
observations["product_count"][0]["agents"] = ["contoso_sales"]
observations["inventory_count"][0]["table"]["metadata"] = {"truncated": True}
observations["hr_salary"][0]["table"]["rows"] = [[0]]
observations["review_pause"][0]["status"] = "success"
hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/"scripts/data").glob("*.json"))}
data_version = "sample-" + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:12]
suite = {"schema_version": 1, "name": "本地业务数据与 HITL 基准", "description": "24 个参考 SQL 结果案例及 6 个对话行为案例。固定样例数据；列名属于输出契约。", "data_version": data_version, "data_files_sha256": hashes, "cases": cases}
replay = {"schema_version": 1, "data_version": data_version, "description": "合成演示输出，包含 5 个故意错误；不是模型执行记录。", "observations": observations}
for name, data in [("suite.json", suite), ("demo_observations.json", replay), ("reference_sql.json", oracle), ("data_manifest.json", hashes)]:
    (OUT/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
for db in databases.values():
    db._engine.dispose()
print(f"Created {len(cases)} cases in {OUT}")
