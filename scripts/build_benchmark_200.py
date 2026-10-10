"""Author 200 cases with executed SQL references and family-disjoint splits."""

import copy
import csv
import hashlib
import json
from pathlib import Path

from data_agent.adapters.sample import create_sample_database
from data_agent.evaluation.models import EvalSuite

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "benchmark200"
FAMILIES = ["基础统计", "条件与边界", "分组与指标", "排序与时间", "关联与子查询"]
DOMAINS = {
    "sales": (
        "contoso_sales",
        "databricks",
        [
            ("统计销售订单数量", "SELECT count(*) AS total FROM orders"),
            ("统计销售客户数量", "SELECT count(*) AS total FROM customers"),
            (
                "计算所有订单金额总和，不过滤状态",
                "SELECT sum(total_amount) AS total FROM orders",
            ),
            (
                "计算所有订单的平均金额，不过滤状态",
                "SELECT avg(total_amount) AS total FROM orders",
            ),
            ("查询最高订单金额", "SELECT max(total_amount) AS total FROM orders"),
            (
                "统计已交付 delivered 的订单数量",
                "SELECT count(*) AS total FROM orders WHERE status='delivered'",
            ),
            (
                "统计金额大于等于 1000 的销售订单数量",
                "SELECT count(*) AS total FROM orders WHERE total_amount>=1000",
            ),
            (
                "统计金额为零的销售订单数量",
                "SELECT count(*) AS total FROM orders WHERE total_amount=0",
            ),
            (
                "统计客户编号为空的订单数量",
                "SELECT count(*) AS total FROM orders WHERE customer_id IS NULL",
            ),
            (
                "查询编号 NOT-EXIST 的订单，返回 order_id",
                "SELECT order_id FROM orders WHERE order_id='NOT-EXIST'",
            ),
            (
                "按订单状态统计订单数",
                "SELECT status,count(*) AS total FROM orders GROUP BY status",
            ),
            (
                "按订单状态统计订单金额总和",
                "SELECT status,sum(total_amount) AS total FROM orders GROUP BY status",
            ),
            (
                "按客户区域统计客户数",
                "SELECT region,count(*) AS total FROM customers GROUP BY region",
            ),
            (
                "按订单状态计算平均订单金额",
                "SELECT status,avg(total_amount) AS total FROM orders GROUP BY status",
            ),
            (
                "查询订单数至少为 5 的客户编号及其订单数",
                "SELECT customer_id,count(*) AS total FROM orders GROUP BY customer_id HAVING count(*)>=5",
            ),
            (
                "列出金额最高的 5 个订单及金额，金额降序、同金额按 order_id 升序",
                "SELECT order_id,total_amount FROM orders ORDER BY total_amount DESC,order_id ASC LIMIT 5",
            ),
            (
                "列出金额最低的 5 个订单及金额，金额升序、同金额按 order_id 升序",
                "SELECT order_id,total_amount FROM orders ORDER BY total_amount ASC,order_id ASC LIMIT 5",
            ),
            (
                "统计 2025 年 1 月的订单数，按 order_date 判断",
                "SELECT count(*) AS total FROM orders WHERE order_date>='2025-01-01' AND order_date<'2025-02-01'",
            ),
            (
                "列出最早的 5 个订单及日期，日期升序、同日按 order_id 升序",
                "SELECT order_id,order_date FROM orders ORDER BY order_date ASC,order_id ASC LIMIT 5",
            ),
            (
                "查询订单日期的最早值和最晚值",
                "SELECT min(order_date) AS first_date,max(order_date) AS last_date FROM orders",
            ),
            (
                "将订单与客户按 customer_id 关联，按客户 region 汇总订单金额",
                "SELECT c.region,sum(o.total_amount) AS total FROM orders o JOIN customers c ON o.customer_id=c.customer_id GROUP BY c.region",
            ),
            (
                "统计没有任何订单的销售客户数量",
                "SELECT count(*) AS total FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id=c.customer_id)",
            ),
            (
                "统计找不到客户记录的订单数量",
                "SELECT count(*) AS total FROM orders o LEFT JOIN customers c ON o.customer_id=c.customer_id WHERE c.customer_id IS NULL",
            ),
            (
                "统计金额高于所有订单平均金额的订单数",
                "SELECT count(*) AS total FROM orders WHERE total_amount>(SELECT avg(total_amount) FROM orders)",
            ),
            (
                "按客户区域计算去重下单客户数，仅统计有订单的客户",
                "SELECT c.region,count(DISTINCT o.customer_id) AS total FROM orders o JOIN customers c ON o.customer_id=c.customer_id GROUP BY c.region",
            ),
        ],
    ),
    "products": (
        "contoso_products",
        "cosmos",
        [
            ("统计产品目录记录数", "SELECT count(*) AS total FROM products"),
            (
                "统计产品目录中的不同分类数",
                "SELECT count(DISTINCT category) AS total FROM products",
            ),
            ("计算目录产品的平均价格", "SELECT avg(price) AS total FROM products"),
            ("查询目录产品最高价格", "SELECT max(price) AS total FROM products"),
            (
                "计算产品目录字段 inventory_count 总和，不查询仓库库存表",
                "SELECT sum(inventory_count) AS total FROM products",
            ),
            (
                "统计产品目录中启用 is_active 为真的产品数",
                "SELECT count(*) AS total FROM products WHERE is_active=1",
            ),
            (
                "统计产品目录中价格为零的产品数",
                "SELECT count(*) AS total FROM products WHERE price=0",
            ),
            (
                "统计产品目录中价格大于等于 100 且小于 500 的产品数",
                "SELECT count(*) AS total FROM products WHERE price>=100 AND price<500",
            ),
            (
                "统计产品目录中 category 为空的产品数",
                "SELECT count(*) AS total FROM products WHERE category IS NULL",
            ),
            (
                "查询目录中 id 为 NOT-EXIST 的产品，返回 id",
                "SELECT id FROM products WHERE id='NOT-EXIST'",
            ),
            (
                "按目录产品分类统计产品数",
                "SELECT category,count(*) AS total FROM products GROUP BY category",
            ),
            (
                "按目录产品分类计算平均价格",
                "SELECT category,avg(price) AS total FROM products GROUP BY category",
            ),
            (
                "按目录产品分类计算 inventory_count 总和",
                "SELECT category,sum(inventory_count) AS total FROM products GROUP BY category",
            ),
            (
                "按目录产品是否启用统计产品数",
                "SELECT is_active,count(*) AS total FROM products GROUP BY is_active",
            ),
            (
                "按目录产品分类计算最高价与最低价",
                "SELECT category,max(price) AS max_price,min(price) AS min_price FROM products GROUP BY category",
            ),
            (
                "列出目录中价格最高的 5 个产品 id 和 price，价格降序、同价 id 升序",
                "SELECT id,price FROM products ORDER BY price DESC,id ASC LIMIT 5",
            ),
            (
                "列出目录中价格最低的 5 个产品 id 和 price，价格升序、同价 id 升序",
                "SELECT id,price FROM products ORDER BY price ASC,id ASC LIMIT 5",
            ),
            (
                "统计目录中 2025 年创建的产品数，按 created_at 判断",
                "SELECT count(*) AS total FROM products WHERE created_at>='2025-01-01' AND created_at<'2026-01-01'",
            ),
            (
                "列出目录最近创建的 5 个产品 id 和 created_at，时间降序、同时间 id 升序",
                "SELECT id,created_at FROM products ORDER BY created_at DESC,id ASC LIMIT 5",
            ),
            (
                "按产品创建年份统计目录产品数，年份取 created_at 前四位",
                "SELECT substr(created_at,1,4) AS year,count(*) AS total FROM products GROUP BY substr(created_at,1,4)",
            ),
            (
                "统计目录中价格高于全部产品平均价的产品数",
                "SELECT count(*) AS total FROM products WHERE price>(SELECT avg(price) FROM products)",
            ),
            (
                "按分类列出平均价高于 100 的分类和平均价格",
                "SELECT category,avg(price) AS total FROM products GROUP BY category HAVING avg(price)>100",
            ),
            (
                "统计目录中 tags 数组为空的产品数，使用本地 SQLite JSON 数组长度",
                "SELECT count(*) AS total FROM products WHERE json_array_length(tags)=0",
            ),
            (
                "按目录分类统计 variants 数组中变体条目总数，使用 SQLite JSON 数组长度",
                "SELECT category,sum(json_array_length(variants)) AS total FROM products GROUP BY category",
            ),
            (
                "列出分类中价格达到该分类最高价的产品 id、category 和 price，包含并列最高价",
                "SELECT p.id,p.category,p.price FROM products p WHERE p.price=(SELECT max(p2.price) FROM products p2 WHERE p2.category=p.category)",
            ),
        ],
    ),
    "inventory": (
        "contoso_inventory",
        "postgres",
        [
            ("统计仓库表中的仓库数", "SELECT count(*) AS total FROM warehouses"),
            (
                "计算全部仓库 capacity 总和",
                "SELECT sum(capacity) AS total FROM warehouses",
            ),
            (
                "计算库存表 quantity 总和",
                "SELECT sum(quantity) AS total FROM inventory",
            ),
            (
                "统计库存表中 product_id 的不同值数量",
                "SELECT count(DISTINCT product_id) AS total FROM inventory",
            ),
            (
                "统计仓库发运 shipments 记录数",
                "SELECT count(*) AS total FROM shipments",
            ),
            (
                "统计 quantity 小于 reorder_level 的库存记录数",
                "SELECT count(*) AS total FROM inventory WHERE quantity<reorder_level",
            ),
            (
                "统计 quantity 为零的库存记录数",
                "SELECT count(*) AS total FROM inventory WHERE quantity=0",
            ),
            (
                "统计 quantity 等于 reorder_level 的库存记录数",
                "SELECT count(*) AS total FROM inventory WHERE quantity=reorder_level",
            ),
            (
                "统计 actual_date 为空的发运记录数",
                "SELECT count(*) AS total FROM shipments WHERE actual_date IS NULL",
            ),
            (
                "查询 product_id 为 NOT-EXIST 的库存记录，返回 inventory_id",
                "SELECT inventory_id FROM inventory WHERE product_id='NOT-EXIST'",
            ),
            (
                "按 warehouse_id 汇总库存 quantity",
                "SELECT warehouse_id,sum(quantity) AS total FROM inventory GROUP BY warehouse_id",
            ),
            (
                "按 warehouse_id 统计低于补货线的库存记录数，仅列出存在低库存的仓库",
                "SELECT warehouse_id,count(*) AS total FROM inventory WHERE quantity<reorder_level GROUP BY warehouse_id",
            ),
            (
                "按发运 status 统计 shipments 记录数",
                "SELECT status,count(*) AS total FROM shipments GROUP BY status",
            ),
            (
                "按 shipment_type 统计 shipments 记录数",
                "SELECT shipment_type,count(*) AS total FROM shipments GROUP BY shipment_type",
            ),
            (
                "按仓库 is_active 统计仓库数",
                "SELECT is_active,count(*) AS total FROM warehouses GROUP BY is_active",
            ),
            (
                "列出 quantity 最低的 5 个库存记录的 inventory_id 与 quantity，数量升序、同数量 ID 升序",
                "SELECT inventory_id,quantity FROM inventory ORDER BY quantity ASC,inventory_id ASC LIMIT 5",
            ),
            (
                "列出 quantity 最高的 5 个库存记录的 inventory_id 与 quantity，数量降序、同数量 ID 升序",
                "SELECT inventory_id,quantity FROM inventory ORDER BY quantity DESC,inventory_id ASC LIMIT 5",
            ),
            (
                "统计 2025 年 12 月计划发运的记录数，按 scheduled_date 判断",
                "SELECT count(*) AS total FROM shipments WHERE scheduled_date>='2025-12-01' AND scheduled_date<'2026-01-01'",
            ),
            (
                "列出容量最大的 3 个仓库编号和容量，capacity 降序、同容量编号升序",
                "SELECT warehouse_id,capacity FROM warehouses ORDER BY capacity DESC,warehouse_id ASC LIMIT 3",
            ),
            (
                "统计实际发运日期晚于计划日期的 shipments 记录数，仅比较非空日期",
                "SELECT count(*) AS total FROM shipments WHERE actual_date IS NOT NULL AND actual_date>scheduled_date",
            ),
            (
                "关联库存和仓库，按仓库 location 汇总 quantity",
                "SELECT w.location,sum(i.quantity) AS total FROM inventory i JOIN warehouses w ON i.warehouse_id=w.warehouse_id GROUP BY w.location",
            ),
            (
                "统计停用仓库中的库存 quantity 总和",
                "SELECT sum(i.quantity) AS total FROM inventory i JOIN warehouses w ON i.warehouse_id=w.warehouse_id WHERE w.is_active=0",
            ),
            (
                "统计没有库存记录的仓库数",
                "SELECT count(*) AS total FROM warehouses w WHERE NOT EXISTS (SELECT 1 FROM inventory i WHERE i.warehouse_id=w.warehouse_id)",
            ),
            (
                "统计找不到对应仓库的库存记录数",
                "SELECT count(*) AS total FROM inventory i LEFT JOIN warehouses w ON i.warehouse_id=w.warehouse_id WHERE w.warehouse_id IS NULL",
            ),
            (
                "统计 quantity 高于全部库存记录平均值的记录数",
                "SELECT count(*) AS total FROM inventory WHERE quantity>(SELECT avg(quantity) FROM inventory)",
            ),
        ],
    ),
    "hr": (
        "contoso_hr",
        "azure_sql",
        [
            ("统计员工记录数", "SELECT count(*) AS total FROM employees"),
            ("统计人事部门数", "SELECT count(*) AS total FROM departments"),
            (
                "计算全部员工 Salary 平均值",
                "SELECT avg(Salary) AS total FROM employees",
            ),
            ("计算全部员工 Salary 总和", "SELECT sum(Salary) AS total FROM employees"),
            ("统计绩效评审记录数", "SELECT count(*) AS total FROM performance_reviews"),
            (
                "统计 IsActive 为真的在职员工数",
                "SELECT count(*) AS total FROM employees WHERE IsActive=1",
            ),
            (
                "统计 Salary 大于等于 100000 的员工数",
                "SELECT count(*) AS total FROM employees WHERE Salary>=100000",
            ),
            (
                "统计 ManagerID 为空的员工数",
                "SELECT count(*) AS total FROM employees WHERE ManagerID IS NULL",
            ),
            (
                "统计状态为 Approved 的请假申请数",
                "SELECT count(*) AS total FROM time_off_requests WHERE Status='Approved'",
            ),
            (
                "查询 EmployeeID 为 -1 的员工，返回 EmployeeID",
                "SELECT EmployeeID FROM employees WHERE EmployeeID=-1",
            ),
            (
                "按 DepartmentID 统计员工数",
                "SELECT DepartmentID,count(*) AS total FROM employees GROUP BY DepartmentID",
            ),
            (
                "按 DepartmentID 计算员工平均 Salary",
                "SELECT DepartmentID,avg(Salary) AS total FROM employees GROUP BY DepartmentID",
            ),
            (
                "按绩效 Rating 统计评审数",
                "SELECT Rating,count(*) AS total FROM performance_reviews GROUP BY Rating",
            ),
            (
                "按请假 Type 汇总 DaysRequested",
                "SELECT Type,sum(DaysRequested) AS total FROM time_off_requests GROUP BY Type",
            ),
            (
                "按员工 Title 统计员工数",
                "SELECT Title,count(*) AS total FROM employees GROUP BY Title",
            ),
            (
                "列出工资最高的 5 个员工 EmployeeID 和 Salary，工资降序、同工资 ID 升序",
                "SELECT EmployeeID,Salary FROM employees ORDER BY Salary DESC,EmployeeID ASC LIMIT 5",
            ),
            (
                "列出工资最低的 5 个员工 EmployeeID 和 Salary，工资升序、同工资 ID 升序",
                "SELECT EmployeeID,Salary FROM employees ORDER BY Salary ASC,EmployeeID ASC LIMIT 5",
            ),
            (
                "统计 2020 年及以后入职的员工数，按 HireDate 判断",
                "SELECT count(*) AS total FROM employees WHERE HireDate>='2020-01-01'",
            ),
            (
                "按入职年份统计员工数，年份取 HireDate 前四位",
                "SELECT substr(HireDate,1,4) AS year,count(*) AS total FROM employees GROUP BY substr(HireDate,1,4)",
            ),
            (
                "统计 2024 年绩效评审数量，按 ReviewDate 判断",
                "SELECT count(*) AS total FROM performance_reviews WHERE ReviewDate>='2024-01-01' AND ReviewDate<'2025-01-01'",
            ),
            (
                "关联部门与员工，按部门 Name 汇总员工 Salary",
                "SELECT d.Name,sum(e.Salary) AS total FROM employees e JOIN departments d ON e.DepartmentID=d.DepartmentID GROUP BY d.Name",
            ),
            (
                "统计没有员工的部门数",
                "SELECT count(*) AS total FROM departments d WHERE NOT EXISTS (SELECT 1 FROM employees e WHERE e.DepartmentID=d.DepartmentID)",
            ),
            (
                "统计工资高于全部员工平均工资的员工数",
                "SELECT count(*) AS total FROM employees WHERE Salary>(SELECT avg(Salary) FROM employees)",
            ),
            (
                "关联绩效与员工，按员工 DepartmentID 计算平均 Rating",
                "SELECT e.DepartmentID,avg(p.Rating) AS total FROM performance_reviews p JOIN employees e ON p.EmployeeID=e.EmployeeID GROUP BY e.DepartmentID",
            ),
            (
                "关联请假与员工，按员工 DepartmentID 汇总 Approved 申请的 DaysRequested",
                "SELECT e.DepartmentID,sum(t.DaysRequested) AS total FROM time_off_requests t JOIN employees e ON t.EmployeeID=e.EmployeeID WHERE t.Status='Approved' GROUP BY e.DepartmentID",
            ),
        ],
    ),
    "finance": (
        "financial_transactions",
        "bigquery",
        [
            ("统计金融账户数", "SELECT count(*) AS total FROM accounts"),
            ("统计金融客户数", "SELECT count(*) AS total FROM customers"),
            (
                "计算金融账户 current_balance 总和，不折算币种",
                "SELECT sum(current_balance) AS total FROM accounts",
            ),
            ("统计金融交易记录数", "SELECT count(*) AS total FROM transactions"),
            ("统计欺诈告警记录数", "SELECT count(*) AS total FROM fraud_alerts"),
            (
                "统计 credit_limit 为空的金融账户数",
                "SELECT count(*) AS total FROM accounts WHERE credit_limit IS NULL",
            ),
            (
                "统计金融账户 current_balance 为负的账户数",
                "SELECT count(*) AS total FROM accounts WHERE current_balance<0",
            ),
            (
                "统计金融客户 risk_score 大于等于 50 的客户数",
                "SELECT count(*) AS total FROM customers WHERE risk_score>=50",
            ),
            (
                "统计 status 为 Completed 的金融交易数",
                "SELECT count(*) AS total FROM transactions WHERE status='Completed'",
            ),
            (
                "查询 account_id 为 NOT-EXIST 的金融账户，返回 account_id",
                "SELECT account_id FROM accounts WHERE account_id='NOT-EXIST'",
            ),
            (
                "按金融账户 account_type 统计账户数",
                "SELECT account_type,count(*) AS total FROM accounts GROUP BY account_type",
            ),
            (
                "按金融交易 channel 统计交易数",
                "SELECT channel,count(*) AS total FROM transactions GROUP BY channel",
            ),
            (
                "按金融交易 currency 汇总原币 amount，不做汇率折算",
                "SELECT currency,sum(amount) AS total FROM transactions GROUP BY currency",
            ),
            (
                "按金融客户 customer_segment 统计客户数",
                "SELECT customer_segment,count(*) AS total FROM customers GROUP BY customer_segment",
            ),
            (
                "按欺诈告警 severity 统计告警数",
                "SELECT severity,count(*) AS total FROM fraud_alerts GROUP BY severity",
            ),
            (
                "列出原币 amount 最高的 5 条金融交易 transaction_id 和 amount，不折算币种，金额降序同金额 ID 升序",
                "SELECT transaction_id,amount FROM transactions ORDER BY amount DESC,transaction_id ASC LIMIT 5",
            ),
            (
                "列出原币 amount 最低的 5 条金融交易 transaction_id 和 amount，不折算币种，金额升序同金额 ID 升序",
                "SELECT transaction_id,amount FROM transactions ORDER BY amount ASC,transaction_id ASC LIMIT 5",
            ),
            (
                "统计 2024 年 12 月的金融交易数，按 transaction_date 判断",
                "SELECT count(*) AS total FROM transactions WHERE transaction_date>='2024-12-01' AND transaction_date<'2025-01-01'",
            ),
            (
                "按金融交易 transaction_date 统计交易数",
                "SELECT transaction_date,count(*) AS total FROM transactions GROUP BY transaction_date",
            ),
            (
                "按金融 monthly_summaries 的 year_month 汇总 transaction_count",
                "SELECT year_month,sum(transaction_count) AS total FROM monthly_summaries GROUP BY year_month",
            ),
            (
                "关联金融账户与客户，按 customer_segment 汇总 current_balance，不折算币种",
                "SELECT c.customer_segment,sum(a.current_balance) AS total FROM accounts a JOIN customers c ON a.customer_id=c.customer_id GROUP BY c.customer_segment",
            ),
            (
                "统计没有任何金融账户的客户数",
                "SELECT count(*) AS total FROM customers c WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.customer_id=c.customer_id)",
            ),
            (
                "关联金融交易与账户，按 account_type 统计交易数",
                "SELECT a.account_type,count(*) AS total FROM transactions t JOIN accounts a ON t.account_id=a.account_id GROUP BY a.account_type",
            ),
            (
                "关联欺诈告警与交易，按交易 channel 统计告警数",
                "SELECT t.channel,count(*) AS total FROM fraud_alerts f JOIN transactions t ON f.transaction_id=t.transaction_id GROUP BY t.channel",
            ),
            (
                "统计找不到对应账户的金融交易数",
                "SELECT count(*) AS total FROM transactions t LEFT JOIN accounts a ON t.account_id=a.account_id WHERE a.account_id IS NULL",
            ),
        ],
    ),
    "hotel": (
        "hotel_analytics",
        "synapse",
        [
            ("统计酒店数量", "SELECT count(*) AS total FROM dim_hotel"),
            ("统计酒店客人数量", "SELECT count(*) AS total FROM dim_guest"),
            ("统计酒店预订记录数", "SELECT count(*) AS total FROM fact_reservations"),
            (
                "计算酒店预订 TotalRevenue 总和，不过滤取消状态",
                "SELECT sum(TotalRevenue) AS total FROM fact_reservations",
            ),
            (
                "计算酒店预订 NightsStayed 总和，不过滤取消状态",
                "SELECT sum(NightsStayed) AS total FROM fact_reservations",
            ),
            (
                "统计 IsCancelled 为真的酒店预订数",
                "SELECT count(*) AS total FROM fact_reservations WHERE IsCancelled=1",
            ),
            (
                "统计未取消酒店预订的 TotalRevenue 总和",
                "SELECT sum(TotalRevenue) AS total FROM fact_reservations WHERE IsCancelled=0",
            ),
            (
                "统计有泳池 HasPool 为真的酒店数",
                "SELECT count(*) AS total FROM dim_hotel WHERE HasPool=1",
            ),
            (
                "统计未取消酒店预订的 CancellationReason 为空的记录数",
                "SELECT count(*) AS total FROM fact_reservations WHERE IsCancelled=0 AND CancellationReason IS NULL",
            ),
            (
                "查询 HotelKey 为 -1 的酒店，返回 HotelKey",
                "SELECT HotelKey FROM dim_hotel WHERE HotelKey=-1",
            ),
            (
                "按酒店 Region 统计酒店数",
                "SELECT Region,count(*) AS total FROM dim_hotel GROUP BY Region",
            ),
            (
                "按预订 HotelKey 汇总全部预订 TotalRevenue",
                "SELECT HotelKey,sum(TotalRevenue) AS total FROM fact_reservations GROUP BY HotelKey",
            ),
            (
                "按酒店 StarRating 汇总 TotalRooms",
                "SELECT StarRating,sum(TotalRooms) AS total FROM dim_hotel GROUP BY StarRating",
            ),
            (
                "按客人 LoyaltyTier 统计客人数",
                "SELECT LoyaltyTier,count(*) AS total FROM dim_guest GROUP BY LoyaltyTier",
            ),
            (
                "按预订 ChannelKey 计算平均 RoomRate，不过滤取消状态",
                "SELECT ChannelKey,avg(RoomRate) AS total FROM fact_reservations GROUP BY ChannelKey",
            ),
            (
                "列出 TotalRevenue 最高的 5 条酒店预订 ReservationKey 和 TotalRevenue，收入降序同收入 ID 升序",
                "SELECT ReservationKey,TotalRevenue FROM fact_reservations ORDER BY TotalRevenue DESC,ReservationKey ASC LIMIT 5",
            ),
            (
                "列出 TotalRevenue 最低的 5 条酒店预订 ReservationKey 和 TotalRevenue，收入升序同收入 ID 升序",
                "SELECT ReservationKey,TotalRevenue FROM fact_reservations ORDER BY TotalRevenue ASC,ReservationKey ASC LIMIT 5",
            ),
            (
                "统计 CheckInDateKey 在 20250101 至 20250131 闭区间的酒店预订数",
                "SELECT count(*) AS total FROM fact_reservations WHERE CheckInDateKey BETWEEN 20250101 AND 20250131",
            ),
            (
                "列出客房数量最多的 3 个酒店 HotelKey 和 TotalRooms，客房数降序同数量 ID 升序",
                "SELECT HotelKey,TotalRooms FROM dim_hotel ORDER BY TotalRooms DESC,HotelKey ASC LIMIT 3",
            ),
            (
                "关联入住日期维度，按 dim_date.Year 汇总酒店预订 TotalRevenue，不过滤取消状态",
                "SELECT d.Year,sum(r.TotalRevenue) AS total FROM fact_reservations r JOIN dim_date d ON r.CheckInDateKey=d.DateKey GROUP BY d.Year",
            ),
            (
                "关联酒店和预订，按酒店 Region 汇总未取消预订 TotalRevenue",
                "SELECT h.Region,sum(r.TotalRevenue) AS total FROM fact_reservations r JOIN dim_hotel h ON r.HotelKey=h.HotelKey WHERE r.IsCancelled=0 GROUP BY h.Region",
            ),
            (
                "统计没有任何预订记录的酒店数",
                "SELECT count(*) AS total FROM dim_hotel h WHERE NOT EXISTS (SELECT 1 FROM fact_reservations r WHERE r.HotelKey=h.HotelKey)",
            ),
            (
                "关联渠道和预订，按 ChannelName 统计全部预订数",
                "SELECT c.ChannelName,count(*) AS total FROM fact_reservations r JOIN dim_booking_channel c ON r.ChannelKey=c.ChannelKey GROUP BY c.ChannelName",
            ),
            (
                "关联客人和预订，按 LoyaltyTier 汇总全部预订 NightsStayed",
                "SELECT g.LoyaltyTier,sum(r.NightsStayed) AS total FROM fact_reservations r JOIN dim_guest g ON r.GuestKey=g.GuestKey GROUP BY g.LoyaltyTier",
            ),
            (
                "关联房型和预订，按 RoomTypeName 计算未取消预订平均 RoomRate",
                "SELECT t.RoomTypeName,avg(r.RoomRate) AS total FROM fact_reservations r JOIN dim_room_type t ON r.RoomTypeKey=t.RoomTypeKey WHERE r.IsCancelled=0 GROUP BY t.RoomTypeName",
            ),
        ],
    ),
}


def write(name, value):
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    cases, oracle, replay, coverage, groups = [], {}, {}, [], {}
    databases, gold = {}, {}
    seen_sql = set()
    try:
        for domain, (agent, source, queries) in DOMAINS.items():
            assert len(queries) == 25
            db = databases[source] = create_sample_database(
                str(ROOT / "scripts/data" / f"{source}_data.json")
            )
            for index, (question, sql) in enumerate(queries):
                key = (source, " ".join(sql.lower().split()))
                if key in seen_sql:
                    raise ValueError(f"Duplicate reference query: {domain} {index}")
                seen_sql.add(key)
                with db._engine.connect() as conn:
                    cursor = conn.exec_driver_sql(sql)
                    columns, rows = list(cursor.keys()), [list(row) for row in cursor]
                if len(rows) > 200:
                    raise ValueError("Reference exceeds evaluation table bound")
                ordered = "ORDER BY" in sql
                table = {"columns": columns, "rows": rows, "ordered": ordered}
                expect = {"status": "success", "agents": [agent], "table": table}
                question += (
                    "。结果列名必须为 "
                    + ", ".join(columns)
                    + "；只查询以上要求的范围。"
                )
                case_id = f"{domain}_{index + 1:02d}"
                group = f"{domain}-{index // 5}"
                groups[case_id] = group
                gold[case_id] = (question, expect, sql, source)
                cases.append({
                    "id": case_id,
                    "category": FAMILIES[index // 5],
                    "description": f"{domain} / {FAMILIES[index // 5]} / 先审阅再执行",
                    "steps": [
                        {
                            "question": question,
                            "expect": {"status": "plan_review", "agents": [agent]},
                        },
                        {"resume": {"action": "approve"}, "expect": expect},
                    ],
                })
                oracle[case_id] = [
                    {
                        "step": 2,
                        "agent": agent,
                        "source": f"{source}_data.json",
                        "sql": sql,
                        "reference_table": table,
                    }
                ]
                replay[case_id] = [
                    {"status": "plan_review", "agents": [agent]},
                    {
                        "status": "success",
                        "agents": [agent],
                        "table": {
                            "columns": columns,
                            "rows": rows,
                            "row_count": len(rows),
                        },
                        "sql": sql,
                    },
                ]
                coverage.append({
                    "id": case_id,
                    "domain": domain,
                    "family": FAMILIES[index // 5],
                    "kind": "single",
                    "group": group,
                    "split": "development" if index // 5 < 3 else "acceptance",
                    "reference_count": 1,
                    "reference_verified": True,
                })

        # Each pair changes the requested measure/order in the same domain and shares its split family.
        for domain, (agent, _, _) in DOMAINS.items():
            for family in range(5):
                first, second = (
                    f"{domain}_{family * 5 + 1:02d}",
                    f"{domain}_{family * 5 + 2:02d}",
                )
                b = gold[second]
                case_id = f"{domain}_followup_{family + 1}"
                group = groups[first]
                groups[case_id] = group
                first_case = next(c for c in cases if c["id"] == first)
                second_case = next(c for c in cases if c["id"] == second)
                steps = copy.deepcopy(first_case["steps"] + second_case["steps"])
                prefix = (
                    "改为按相同数据源返回最低的项目，"
                    if family == 3
                    else "继续刚才的数据源，改查下面这个口径，"
                )
                steps[2]["question"] = prefix + b[0]
                cases.append({
                    "id": case_id,
                    "category": "多轮任务切换",
                    "description": f"{domain} / {FAMILIES[family]} / 同一会话更改查询",
                    "steps": steps,
                })
                oracle[case_id] = [
                    copy.deepcopy(oracle[first][0]),
                    {**copy.deepcopy(oracle[second][0]), "step": 4},
                ]
                replay[case_id] = copy.deepcopy(replay[first] + replay[second])
                coverage.append({
                    "id": case_id,
                    "domain": domain,
                    "family": FAMILIES[family],
                    "kind": "multi_turn",
                    "group": group,
                    "split": "development" if family < 3 else "acceptance",
                    "reference_count": 2,
                    "reference_verified": True,
                })

        # These 20 cases test distinct interaction paths over five cross-source scenarios.
        pairs = [
            ("sales_03", "products_03"),
            ("inventory_02", "hr_04"),
            ("finance_04", "hotel_03"),
            ("sales_18", "finance_18"),
            ("hr_23", "products_21"),
        ]
        for index, (left, right) in enumerate(pairs):
            lq, le, ls, lsource = gold[left]
            rq, re, rs, rsource = gold[right]
            agents = sorted(le["agents"] + re["agents"])
            question = f"分别完成两个查询，分开报告，不关联、不合并表格、不计算两者差值或比率：第一项 {lq} 第二项 {rq}"
            pending = {"status": "plan_review", "agents": agents}
            source_group = groups[left]
            # Both references in a pair belong to the same development/acceptance half.
            assert (int(groups[left].rsplit("-", 1)[1]) < 3) == (
                int(groups[right].rsplit("-", 1)[1]) < 3
            )
            for action in ("pause", "cancel", "approve", "revise"):
                case_id = f"collaboration_{index + 1}_{action}"
                groups[case_id] = f"pair-{index}"
                steps = [{"question": question, "expect": pending}]
                outputs = [copy.deepcopy(pending)]
                references = []
                if action == "cancel":
                    steps.append({
                        "resume": {"action": "cancel"},
                        "expect": {
                            "status": "cancelled",
                            "error_contains": "cancelled",
                        },
                    })
                    outputs.append({"status": "cancelled", "error": "cancelled"})
                elif action == "approve":
                    spec = {
                        "status": "success",
                        "agents": agents,
                        "agent_tables": {
                            le["agents"][0]: le["table"],
                            re["agents"][0]: re["table"],
                        },
                    }
                    steps.append({"resume": {"action": "approve"}, "expect": spec})
                    outputs.append({
                        "status": "success",
                        "agents": agents,
                        "evidence": [
                            {
                                "task_id": "left",
                                "agent_name": le["agents"][0],
                                "status": "success",
                                "result": {
                                    **le["table"],
                                    "row_count": len(le["table"]["rows"]),
                                },
                            },
                            {
                                "task_id": "right",
                                "agent_name": re["agents"][0],
                                "status": "success",
                                "result": {
                                    **re["table"],
                                    "row_count": len(re["table"]["rows"]),
                                },
                            },
                        ],
                    })
                    references = [
                        {**copy.deepcopy(oracle[left][0]), "step": 2},
                        {**copy.deepcopy(oracle[right][0]), "step": 2},
                    ]
                elif action == "revise":
                    revised = {"status": "plan_review", "agents": le["agents"]}
                    steps.append({
                        "resume": {
                            "action": "revise",
                            "feedback": "删除第二项，只保留第一项查询：" + lq,
                        },
                        "expect": revised,
                    })
                    steps.append({"resume": {"action": "approve"}, "expect": le})
                    outputs.extend([revised, copy.deepcopy(replay[left][1])])
                    references = [{**copy.deepcopy(oracle[left][0]), "step": 3}]
                cases.append({
                    "id": case_id,
                    "category": "协作与审批",
                    "description": f"跨源场景 {index + 1} / {action}",
                    "steps": steps,
                })
                oracle[case_id], replay[case_id] = references, outputs
                coverage.append({
                    "id": case_id,
                    "domain": "cross_source",
                    "family": f"交互-{action}",
                    "kind": "interaction",
                    "group": f"pair-{index}",
                    "split": "development"
                    if int(source_group.rsplit("-", 1)[1]) < 3
                    else "acceptance",
                    "reference_count": len(references),
                    "reference_verified": bool(references),
                })

        assert len(cases) == 200 and len({c["id"] for c in cases}) == 200
        hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "scripts/data").glob("*.json"))
        }
        version = (
            "benchmark200-v1-"
            + hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[
                :12
            ]
        )
        suite = {
            "schema_version": 1,
            "name": "多数据源 Agent 200 案例基准 v1",
            "description": "150 个独立查询，30 个同域多轮任务切换，20 个跨源审批交互。review_policy=always；所有确认均在题目内显式编写。",
            "data_version": version,
            "data_files_sha256": hashes,
            "cases": cases,
        }
        EvalSuite.model_validate(suite)
        split_by_id = {row["id"]: row["split"] for row in coverage}
        split_queries = {split: set() for split in ("development", "acceptance")}
        for case_id, references in oracle.items():
            for reference in references:
                split_queries[split_by_id[case_id]].add((
                    reference["source"],
                    " ".join(reference["sql"].lower().split()),
                ))
        if split_queries["development"] & split_queries["acceptance"]:
            raise ValueError("Reference query leaks across splits")
        write("suite.json", suite)
        for split in ("development", "acceptance"):
            subset = {
                **suite,
                "name": suite["name"] + " " + split,
                "cases": [c for c in cases if split_by_id[c["id"]] == split],
            }
            EvalSuite.model_validate(subset)
            write(f"{split}.json", subset)
        smoke_ids = [f"{domain}_03" for domain in DOMAINS] + [
            "sales_followup_1",
            "collaboration_1_approve",
            "collaboration_2_revise",
            "collaboration_3_cancel",
        ]
        write(
            "smoke.json",
            {
                **suite,
                "name": suite["name"] + " smoke",
                "cases": [c for c in cases if c["id"] in smoke_ids],
            },
        )
        write("reference_sql.json", oracle)
        write("data_manifest.json", hashes)
        write(
            "oracle_replay.json",
            {
                "schema_version": 1,
                "data_version": version,
                "description": "由参考答案构造的正向评分器验证数据；不是模型输出，也不是模型能力成绩。",
                "observations": replay,
            },
        )
        negative = copy.deepcopy(replay)
        negative["sales_01"][1]["table"]["rows"] = [[-999]]
        negative["products_01"][1]["agents"] = ["contoso_sales"]
        negative["inventory_01"][1]["table"]["metadata"] = {"truncated": True}
        negative["hr_01"][0]["status"] = "success"
        negative["finance_01"][1]["table"]["row_count"] = 999
        negative["hotel_16"][1]["table"]["rows"].reverse()
        negative["collaboration_1_approve"][1]["evidence"][0]["result"]["rows"] = [
            [-999]
        ]
        negative["sales_followup_1"][3]["table"]["rows"] = [[-999]]
        write(
            "negative_replay.json",
            {
                "schema_version": 1,
                "data_version": version,
                "description": "8 个故意错误的评分器负向验证；预期 192/200，通过率不是模型能力。",
                "observations": negative,
            },
        )
        with (OUT / "coverage.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=list(coverage[0]))
            writer.writeheader()
            writer.writerows(coverage)
        write(
            "audit.json",
            {
                "case_count": len(cases),
                "step_count": sum(len(c["steps"]) for c in cases),
                "unique_reference_queries": len(seen_sql),
                "kind_counts": {
                    kind: sum(row["kind"] == kind for row in coverage)
                    for kind in ("single", "multi_turn", "interaction")
                },
                "split_counts": {
                    split: sum(row["split"] == split for row in coverage)
                    for split in split_queries
                },
                "cross_split_reference_overlap": 0,
                "reference_dialect": "sqlite",
                "review_policy": "always",
                "human_review": "pending; reference SQL constructed and execution verified, not externally human labelled",
                "independence_note": "Multi-turn and interaction cases reuse some single-query references; 200 scenarios are not 200 independent SQL tasks.",
            },
        )
        print(
            f"Built {len(cases)} cases, {len(seen_sql)} unique reference queries in {OUT}"
        )
    finally:
        for database in databases.values():
            database._engine.dispose()


if __name__ == "__main__":
    build()
