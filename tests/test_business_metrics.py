"""Business dictionary availability, replacement and per-source isolation."""

import json

import pytest

from data_agent.collaboration import AgentCoordinator
from data_agent.context import (
    BUSINESS_METRICS,
    ContextUpdate,
    SchemaSnapshot,
    business_metrics,
    metric_catalog,
    metric_definitions,
    prepare_context,
)


def snapshot(table, columns):
    return SchemaSnapshot("schema", "v1", ((table, tuple(columns)),), "test")


def test_missing_reserved_inventory_is_visible_but_not_selectable():
    schema = snapshot("inventory", ["quantity", "product_id", "warehouse_id"])
    entry = next(m for m in metric_catalog(schema) if m["id"] == "available_inventory")
    assert entry["missing_columns"] == ["reserved_quantity"]
    assert not entry["available"]
    assert "available_inventory" not in {m.id for m in business_metrics(schema)}
    with pytest.raises(ValueError, match="Metric unavailable"):
        prepare_context(
            {},
            [ContextUpdate(field="metric_id", value="available_inventory")],
            followup=False,
            question="query",
            agents=["inventory"],
            metrics={"inventory": business_metrics(schema)},
            versions={},
        )


def test_sales_time_capability_and_hr_no_historical_headcount():
    sales = snapshot("orders", ["order_id", "total_amount", "order_date"])
    assert {m.id for m in business_metrics(sales)} == {"order_records", "order_amount"}
    assert all(m["time_filter_supported"] for m in metric_catalog(sales))
    hr = snapshot("employees", ["EmployeeID", "IsActive", "HireDate"])
    assert not metric_catalog(hr)[0]["time_filter_supported"]


def test_reviewed_catalog_replaces_defaults_and_rejects_duplicates(
    tmp_path, monkeypatch
):
    path = tmp_path / "metrics.json"
    metric = next(m for m in BUSINESS_METRICS if m.id == "order_records")
    path.write_text(json.dumps([metric.model_dump()]), encoding="utf-8")
    monkeypatch.setenv("DATA_AGENT_METRICS_FILE", str(path))
    assert [m.id for m in metric_definitions()] == ["order_records"]
    path.write_text(json.dumps([metric.model_dump()] * 2), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        metric_definitions()


def test_sales_metric_kept_but_warehouse_removed():
    coordinator = object.__new__(AgentCoordinator)
    coordinator.metrics = {
        "sales": [m for m in BUSINESS_METRICS if m.table == "orders"]
    }
    raw = {
        "metric_agent": "sales",
        "metric": {"id": "order_amount"},
        "conditions": {
            "metric_id": {"value": "order_amount"},
            "warehouse_scope": {"value": "Shanghai"},
        },
    }
    filtered = coordinator._worker_context(raw, "sales")
    assert filtered["metric"] == raw["metric"]
    assert "warehouse_scope" not in filtered["conditions"]
    assert "warehouse_scope" in raw["conditions"]
