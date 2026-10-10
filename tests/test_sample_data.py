"""Integration checks for local JSON data and configuration routing."""

import json
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from data_agent.adapters.sample import create_sample_database
from data_agent.agent import DataAgentFlow
from data_agent.config import SampleDatasource
from data_agent.config_loader import ConfigLoader


def test_all_sample_sources(monkeypatch):
    monkeypatch.setenv("DATA_AGENT_SAMPLE_MODE", "true")
    config = ConfigLoader.load_all()
    assert len(config.data_agents) == 6
    total = 0
    for agent in config.data_agents:
        assert isinstance(agent.datasource, SampleDatasource)
        assert not agent.table_schemas
        db = DataAgentFlow._create_datasource(None, agent.name, agent.datasource)
        assert db.dialect == "sqlite"
        data = json.loads(Path(agent.datasource.source).read_text(encoding="utf-8"))
        for table, rows in data.items():
            assert db.run(f'SELECT COUNT(*) FROM "{table}"') == str([(len(rows),)])
            total += len(rows)
        with pytest.raises(OperationalError, match="readonly"):
            db.run(f'DELETE FROM "{next(iter(data))}"')
        db._engine.dispose()
    assert total == 720


def test_generated_ids_and_nested_json():
    root = Path(__file__).resolve().parents[1] / "scripts" / "data"
    db = create_sample_database(str(root / "postgres_data.json"))
    assert db.run(
        "SELECT COUNT(*) FROM inventory i JOIN warehouses w "
        "ON i.warehouse_id = w.warehouse_id"
    ) == "[(125,)]"
    db._engine.dispose()
    db = create_sample_database(str(root / "cosmos_data.json"))
    assert db.run("SELECT COUNT(*) FROM products WHERE json_valid(variants)") == "[(45,)]"
    db._engine.dispose()


def test_remote_mode_preserved(monkeypatch):
    monkeypatch.setenv("DATA_AGENT_SAMPLE_MODE", "false")
    config = ConfigLoader.load_by_name("contoso")
    assert [a.datasource.type for a in config.data_agents] == [
        "databricks", "cosmos", "postgres"
    ]
