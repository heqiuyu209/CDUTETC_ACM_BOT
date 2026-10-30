"""旧 schema 并发启动与整个迁移事务回滚的真实 SQLite 回归。"""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

import database


def old_database(path):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""CREATE TABLE contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '',
            reg_deadline TEXT NOT NULL DEFAULT '', contest_time TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
            source_link TEXT NOT NULL DEFAULT '', raw_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '')""")
        conn.execute("INSERT INTO contests (name, raw_note) VALUES ('old contest', 'keep original note')")


def test_two_independent_processes_initialize_old_schema_without_duplicate_alter(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    go = tmp_path / "go"
    root = Path(__file__).resolve().parents[1]
    worker = r"""
import json, sqlite3, sys, time
from contextlib import closing
from pathlib import Path
import database

path, ready, go = map(Path, sys.argv[1:])
class CheckedConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if sql.upper().startswith(('PRAGMA TABLE_INFO', 'ALTER TABLE')):
            assert self.in_transaction, 'schema read/write outside initialization transaction'
        return super().execute(sql, *args, **kwargs)

database._connect = lambda: sqlite3.connect(path, timeout=10, factory=CheckedConnection)
# 两个进程先都到达这里，再进入 init_db；不能在持有锁后的 PRAGMA 上设 Barrier。
ready.write_text('ready')
deadline = time.monotonic() + 10
while not go.exists():
    if time.monotonic() > deadline:
        raise TimeoutError('start gate not released')
    time.sleep(0.01)
database.init_db()
database.init_db()
with closing(sqlite3.connect(path)) as conn:
    row = conn.execute('SELECT id, name, raw_note, group_id FROM contests').fetchall()
    print(json.dumps({'ok': True, 'rows': row}))
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), os.environ.get("PYTHONPATH", ""))),
               PYTHONIOENCODING="utf-8")
    processes = []
    try:
        ready_files = [tmp_path / f"ready-{index}" for index in range(2)]
        for ready in ready_files:
            processes.append(subprocess.Popen(
                [sys.executable, "-c", worker, str(path), str(ready), str(go)],
                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 10
        while not all(ready.exists() for ready in ready_files):
            assert time.monotonic() < deadline, "startup processes did not reach the gate"
            assert all(process.poll() is None for process in processes), "startup worker failed before the gate"
            time.sleep(0.01)
        go.write_text("start")
        results = [process.communicate(timeout=15) for process in processes]
        for process, (out, err) in zip(processes, results):
            assert process.returncode == 0, err
            assert json.loads(out) == {"ok": True, "rows": [[1, "old contest", "keep original note", ""]]}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    with closing(sqlite3.connect(path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(contests)")]
        assert columns.count("group_id") == 1
        for ddl in database._ALL_DDL:
            table = ddl.split("EXISTS", 1)[1].split("(", 1)[0].strip()
            assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()


def test_failed_migration_rolls_back_all_new_tables_and_altered_columns(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    old_database(path)
    opened = []

    def connect():
        conn = sqlite3.connect(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    original = database._migrate_v1

    def fail_after_alter(conn):
        assert conn.in_transaction
        original(conn)
        raise RuntimeError("injected failure after ALTER")

    monkeypatch.setattr(database, "_migrate_v1", fail_after_alter)
    try:
        with pytest.raises(RuntimeError, match="after ALTER"):
            database.init_db()
        with closing(sqlite3.connect(path)) as conn:
            assert "group_id" not in [row[1] for row in conn.execute("PRAGMA table_info(contests)")]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert tables == {"contests", "sqlite_sequence"}
            assert conn.execute("SELECT id, name, raw_note FROM contests").fetchall() == [(1, "old contest", "keep original note")]
        monkeypatch.setattr(database, "_migrate_v1", original)
        database.init_db()
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT group_id, name FROM contests").fetchall() == [("", "old contest")]
    finally:
        for conn in opened:
            conn.close()


def test_existing_caller_transaction_initializes_without_nested_begin(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        database.init_db()
        assert not conn.in_transaction  # 与原 init_db 一样，连接上下文提交调用方事务。
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0
    finally:
        conn.close()
