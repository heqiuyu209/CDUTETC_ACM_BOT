"""仅用隔离 SQLite 验证正式启用前清理，不读写实际云端数据库。"""

from contextlib import closing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cdutetc import database
from scripts import reset_business_data as reset


def test_custom_reminders_is_a_business_table():
    """统一提醒中心的自定义提醒属业务数据：随重置清理，绝不按配置保留。"""
    assert "custom_reminders" in reset.BUSINESS_TABLES
    assert "custom_reminders" not in reset.CONFIG_TABLES


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        return list(conn.iterdump())


@pytest.fixture(params=["delete", "wal"])
def populated_db(tmp_path, monkeypatch, request):
    path = tmp_path / "bot data.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    database.init_db()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA journal_mode={request.param}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {*reset.BUSINESS_TABLES, *reset.CONFIG_TABLES, "sqlite_sequence"}
        for table in (*reset.BUSINESS_TABLES, *reset.CONFIG_TABLES):
            values = {}
            for _, name, kind, required, default, primary in conn.execute(f'PRAGMA table_info("{table}")'):
                if primary and kind == "INTEGER" and name == "id":
                    continue
                if (required or primary) and default is None:
                    values[name] = 1 if kind == "INTEGER" else f"seed-{name}"
            if table == "group_notification_settings":
                values["message_format"] = "markdown"
            if values:
                columns = ", ".join(f'"{name}"' for name in values)
                placeholders = ", ".join("?" for _ in values)
                conn.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(values.values()))
            else:
                conn.execute(f'INSERT INTO "{table}" DEFAULT VALUES')
        conn.commit()
    return path


def test_preview_is_readonly_and_does_not_create_backup(populated_db):
    before = snapshot(populated_db)
    result = reset.reset_business_data(populated_db)
    assert result.database == populated_db.resolve()
    assert sum(result.cleared.values()) == len(reset.BUSINESS_TABLES)
    assert result.backup is None
    assert snapshot(populated_db) == before
    assert not (populated_db.parent / "backups").exists()


def test_apply_clears_every_business_table_backs_up_and_preserves_configuration(populated_db):
    before = snapshot(populated_db)
    result = reset.reset_business_data(populated_db, apply=True)
    assert result.backup.is_file()
    assert snapshot(result.backup) == before
    preview = reset.reset_business_data(populated_db)
    assert set(preview.cleared.values()) == {0}
    assert preview.cleared["custom_reminders"] == 0
    assert preview.preserved == result.preserved
    with closing(sqlite3.connect(populated_db)) as conn, closing(sqlite3.connect(result.backup)) as backup:
        for table in (*reset.CONFIG_TABLES, "sqlite_sequence"):
            assert conn.execute(f'SELECT * FROM "{table}"').fetchall() == backup.execute(f'SELECT * FROM "{table}"').fetchall()
        assert conn.execute("SELECT message_format FROM group_notification_settings").fetchone() == ("markdown",)
        # 旧审批编号绝不能被新申请复用。
        cursor = conn.execute("INSERT INTO pending_publications DEFAULT VALUES")
        assert cursor.lastrowid > backup.execute("SELECT MAX(id) FROM pending_publications").fetchone()[0]
        conn.commit()


def test_backup_failure_never_deletes_and_releases_lock(populated_db, monkeypatch):
    before = snapshot(populated_db)
    def fail(*args):
        raise OSError("backup unavailable")
    monkeypatch.setattr(reset, "_backup", fail)
    with pytest.raises(OSError):
        reset.reset_business_data(populated_db, apply=True)
    assert snapshot(populated_db) == before
    with closing(sqlite3.connect(populated_db, timeout=0.1)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()


def test_late_delete_failure_rolls_back_earlier_deletions_and_leaves_valid_backup(populated_db):
    with closing(sqlite3.connect(populated_db)) as conn:
        conn.execute("CREATE TRIGGER refuse_reset BEFORE DELETE ON competitions BEGIN SELECT RAISE(ABORT, 'refused'); END")
        conn.commit()
    before = snapshot(populated_db)
    with pytest.raises(sqlite3.IntegrityError):
        reset.reset_business_data(populated_db, apply=True)
    assert snapshot(populated_db) == before
    backups = list((populated_db.parent / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    assert snapshot(backups[0]) == before


def test_unexpected_configuration_change_rolls_back(populated_db):
    with closing(sqlite3.connect(populated_db)) as conn:
        conn.execute("CREATE TRIGGER change_configuration AFTER DELETE ON unanswered_questions "
                     "BEGIN DELETE FROM group_notification_settings; END")
        conn.commit()
    before = snapshot(populated_db)
    with pytest.raises(ValueError, match="配置或编号"):
        reset.reset_business_data(populated_db, apply=True)
    assert snapshot(populated_db) == before


def test_unknown_table_prevents_partial_reset(populated_db):
    with closing(sqlite3.connect(populated_db)) as conn:
        conn.execute("CREATE TABLE unknown_business (id INTEGER)")
        conn.commit()
    before = snapshot(populated_db)
    with pytest.raises(ValueError, match="unknown_business"):
        reset.reset_business_data(populated_db, apply=True)
    assert snapshot(populated_db) == before
    assert not (populated_db.parent / "backups").exists()


def test_missing_or_wrong_database_is_not_created_or_cleared(tmp_path):
    missing = tmp_path / "not-found.sqlite3"
    with pytest.raises(FileNotFoundError):
        reset.reset_business_data(missing, apply=True)
    assert not missing.exists()
    with closing(sqlite3.connect(missing)):
        pass
    with pytest.raises(ValueError, match="核对 --db"):
        reset.reset_business_data(missing, apply=True)
    assert not (tmp_path / "backups").exists()


def test_cli_runs_with_standard_library_only_and_no_environment_file_changes(populated_db):
    before = snapshot(populated_db)
    script = Path(reset.__file__).resolve()
    result = subprocess.run(
        [sys.executable, "-S", str(script), "--db", str(populated_db)],
        cwd=populated_db.parent, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "未修改数据" in result.stdout
    assert "seed-" not in result.stdout
    assert snapshot(populated_db) == before
    assert not (populated_db.parent / ".env").exists()


def test_cli_apply_reports_backup_and_errors_return_nonzero(populated_db, capsys):
    assert reset.main(["--db", str(populated_db), "--apply"]) == 0
    output = capsys.readouterr().out
    assert "完整备份" in output and "清理完成" in output
    assert "seed-" not in output
    assert reset.main(["--db", str(populated_db.parent / "missing.sqlite3"), "--apply"]) == 1
