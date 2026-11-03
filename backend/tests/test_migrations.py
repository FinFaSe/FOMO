"""Alembic migrations: models match the migrations, pre-Alembic databases keep their data, locking and checks."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from tracker.db import Base, database, make_engine
from tracker.migrate import BASELINE, SchemaOutdated, alembic_config, current_revision, head_revision, upgrade

from .conftest import b58

BACKEND = Path(__file__).resolve().parents[1]
NEW_TABLES = {"sync_state", "trades", "price_candles", "jobs", "workers", "token_mints", "token_quotes",
              "discovery_seeds", "early_buys", "wallet_funding", "alerts", "alert_state", "pending_signatures"}


def url(tmp_path, name="m.db"):
    return f"sqlite:///{tmp_path}/{name}"


def drift(engine):
    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)


def test_models_match_the_migrations(tmp_path):
    """Fails when a model changes without a new revision (`alembic revision --autogenerate`)."""
    engine = make_engine(url(tmp_path))
    report = upgrade(engine)
    assert report["before"] is None and report["after"] == head_revision() and not report["stamped_baseline"]
    assert drift(engine) == []
    assert upgrade(engine)["before"] == head_revision()  # idempotent


def pre_alembic_database(tmp_path, name="legacy.db"):
    """A database as the imported code left it: the six original tables, data, and no alembic_version."""
    engine = make_engine(url(tmp_path, name))
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), BASELINE)
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("INSERT INTO wallets (address, chain, label, favorite, created_at) "
                          "VALUES (:a, 'solana', 'LEGACY', 1, '2026-10-31T00:00:00+00:00')"), {"a": b58(111)})
        conn.execute(text("INSERT INTO raw_transactions (signature, slot, payload, digest, source, fetched_at) "
                          "VALUES (:s, 1, :p, 'd', 'https://example.org', '2026-10-31T00:00:00+00:00')"),
                     {"s": b58(42, 64), "p": json.dumps({"slot": 1})})
    return engine


def test_pre_alembic_database_is_stamped_and_upgraded_keeping_its_data(tmp_path):
    engine = pre_alembic_database(tmp_path)
    assert NEW_TABLES.isdisjoint(inspect(engine).get_table_names())
    report = upgrade(engine)
    assert report["stamped_baseline"] and report["after"] == head_revision()
    assert NEW_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT label, favorite FROM wallets")).one() == ("LEGACY", 1)
        assert conn.execute(text("SELECT count(*) FROM raw_transactions")).scalar() == 1
    assert drift(engine) == []


def test_intermediate_development_database_gets_missing_columns(tmp_path):
    """A database from an earlier version of this branch: sync_state exists without later columns."""
    engine = pre_alembic_database(tmp_path, "dev.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sync_state (wallet VARCHAR(44) NOT NULL PRIMARY KEY "
                          "REFERENCES wallets(address), head_signature VARCHAR(88), tail_signature VARCHAR(88), "
                          "tail_block_time INTEGER, covered_from INTEGER, history_start_reached BOOLEAN NOT NULL, "
                          "catchup_before VARCHAR(88), catchup_head VARCHAR(88), missing_block_times INTEGER NOT NULL, "
                          "updated_at VARCHAR(40) NOT NULL)"))
        conn.execute(text("INSERT INTO sync_state (wallet, head_signature, history_start_reached, "
                          "missing_block_times, updated_at) VALUES (:w, 'HEAD', 1, 0, 'x')"), {"w": b58(111)})
    upgrade(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("sync_state")}
    assert {"normalizer", "caught_up_at", "watch_head"} <= columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT head_signature FROM sync_state")).scalar() == "HEAD"
    assert drift(engine) == []


def test_auto_migrate_off_refuses_an_outdated_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_AUTO_MIGRATE", "0")
    with pytest.raises(SchemaOutdated):
        database(url(tmp_path))
    upgrade(make_engine(url(tmp_path)))
    engine, _ = database(url(tmp_path))  # now at head: starts normally
    assert current_revision(engine) == head_revision()


def test_downgrade_to_the_baseline_and_back(tmp_path):
    engine = make_engine(url(tmp_path))
    upgrade(engine)
    with engine.begin() as conn:
        command.downgrade(alembic_config(conn), BASELINE)
    tables = set(inspect(engine).get_table_names())
    assert NEW_TABLES.isdisjoint(tables) and "wallets" in tables
    upgrade(engine)
    assert drift(engine) == []


def test_concurrent_upgrades_from_two_processes_are_serialized(tmp_path):
    target = url(tmp_path, "race.db")
    code = "import sys; from tracker.db import make_engine; from tracker.migrate import upgrade; " \
           "print(upgrade(make_engine(sys.argv[1]))['after'])"
    procs = [subprocess.Popen([sys.executable, "-c", code, target], cwd=BACKEND, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), [err for _, err in results]
    assert [out.strip() for out, _ in results] == [head_revision()] * 2


def test_cli_reports_and_upgrades(tmp_path):
    env = {**os.environ, "DATABASE_URL": url(tmp_path, "cli.db")}
    run = lambda action: subprocess.run([sys.executable, "-m", "tracker.migrate", action], cwd=BACKEND, env=env,  # noqa: E731
                                        capture_output=True, text=True, timeout=60)
    assert run("check").returncode != 0
    assert run("upgrade").returncode == 0
    assert head_revision() in run("current").stdout and run("check").stdout.strip() == "ok"
