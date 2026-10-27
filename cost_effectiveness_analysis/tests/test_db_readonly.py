# -*- coding: utf-8 -*-
import sqlite3

import pytest

from cost_effectiveness_analysis.db_reader import ReadOnlySchemeRepository


def _database(path):
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE agreements(agreement_id TEXT PRIMARY KEY,agreement_name TEXT,agreement_source TEXT,
      historical_price_wan REAL,source_year INTEGER,enabled INTEGER,params_json TEXT,capability_score REAL);
    CREATE TABLE saved_schemes(id INTEGER PRIMARY KEY,scheme_name TEXT,source_type TEXT,created_at TEXT,
      params_json TEXT,evaluation_json TEXT);
    CREATE TABLE parameter_definitions(parameter_id TEXT,model_value_mapping_json TEXT,enabled INTEGER);
    INSERT INTO agreements VALUES('H1','历史A','historical',12.5,2025,1,'{"p":"高"}',88);
    INSERT INTO saved_schemes VALUES(1,'专家A','manual','2026-01-01','{"p":"低"}','{}');
    INSERT INTO parameter_definitions VALUES('p','{"高":1,"低":0}',1);
    """)
    conn.commit(); conn.close()


def test_repository_reads_both_sources_and_encodes(tmp_path):
    db = tmp_path / "schemes.db"; _database(db)
    repo = ReadOnlySchemeRepository(db)
    assert len(repo.list_schemes()) == 2
    assert repo.get_scheme("agreement:H1")["parameters"] == {"p":"高"}
    assert repo.get_scheme_parameters("agreement:H1", model_values=True) == {"p":1}


def test_connection_rejects_all_sqlite_writes(tmp_path):
    db = tmp_path / "schemes.db"; _database(db)
    repo = ReadOnlySchemeRepository(db)
    conn = repo._connect()
    try:
        for statement in ("UPDATE agreements SET agreement_name='X'",
                          "INSERT INTO agreements(agreement_id) VALUES('X')",
                          "DELETE FROM agreements", "CREATE TABLE forbidden(x)",
                          "DROP TABLE agreements"):
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(statement)
    finally:
        conn.close()
