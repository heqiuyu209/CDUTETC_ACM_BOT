"""正式启用前清空业务数据：默认预览，--apply 时先完整备份再原子清理。

仅用 Python 标准库，不导入机器人配置、不修改 .env、不启动机器人。
运行前停止机器人服务，并用 --db 明确指定服务实际使用的 SQLite 文件。
"""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import uuid


# 子表、队列先清；不重置 sqlite_sequence，防止旧审批编号指向新记录。
BUSINESS_TABLES = (
    "reminder_success_receipts", "reminder_deliveries", "reminders",
    # 统一提醒中心的自定义提醒：属业务数据，正式启用前随重置一并清空。
    "custom_reminders",
    "collection_responses", "collections", "unanswered_questions", "group_faq",
    "training_sessions", "org_events", "competition_pending_updates",
    "competition_versions", "competition_sources", "competition_notify_groups",
    "weekly_contest_runs", "publication_claims", "pending_publications",
    "competitions", "contests", "audit_log",
)
CONFIG_TABLES = (
    "runtime_allowed_groups", "runtime_teachers", "account_labels", "entity_aliases",
    "group_notification_settings", "group_notification_blocks",
    "group_weekly_settings", "group_teacher_assignments",
    # 群级键值开关（课程提醒开/关等）：属配置，重置业务数据时一并保留。
    "group_settings",
)


@dataclass(frozen=True)
class ResetResult:
    database: Path
    cleared: dict[str, int]
    preserved: dict[str, int]
    backup: Path | None = None


def _connect(path: Path, mode: str) -> sqlite3.Connection:
    # mode=ro/rw 都不会把错误路径静默建成空数据库。
    return sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=5)


def _inventory(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int]]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    unknown = {table for table in tables if not table.startswith("sqlite_")} - set(BUSINESS_TABLES) - set(CONFIG_TABLES)
    if unknown:
        raise ValueError("存在未识别的数据表，请先核对清理范围：" + ", ".join(sorted(unknown)))
    if not {"group_faq", "unanswered_questions"}.issubset(tables):
        raise ValueError("未发现预期的机器人业务表，请核对 --db 路径")
    return tuple({table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                  for table in group if table in tables} for group in (BUSINESS_TABLES, CONFIG_TABLES))


def _backup(path: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"{path.stem}-before-reset-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
    # 备份含完整业务数据；新文件独占创建，Linux 上仅当前用户可读写。
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(_connect(path, "ro")) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("备份校验失败，未执行清理")
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def reset_business_data(db_path: str | Path, *, apply: bool = False,
                        backup_dir: str | Path | None = None) -> ResetResult:
    """服务停止后调用。预览只读；实际清理前备份失败则绝不删除。"""
    path = Path(db_path).expanduser().resolve(strict=True)
    directory = Path(backup_dir).expanduser().resolve() if backup_dir is not None else path.parent / "backups"
    with closing(_connect(path, "rw" if apply else "ro")) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        # 独占写入资格直到清理完成；另一只读连接可以备份当前已提交状态。
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            cleared, preserved = _inventory(conn)
            if not apply:
                return ResetResult(path, cleared, preserved)
            protected = (*preserved, "sqlite_sequence")
            snapshots = {table: conn.execute(f'SELECT * FROM "{table}"').fetchall()
                         for table in protected
                         if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()}
            backup = _backup(path, directory)
            for table in cleared:
                conn.execute(f'DELETE FROM "{table}"')
            if any(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in cleared):
                raise ValueError("业务表清理不完整，已回滚")
            if any(conn.execute(f'SELECT * FROM "{table}"').fetchall() != rows
                   for table, rows in snapshots.items()):
                raise ValueError("检测到配置或编号发生变化，已回滚")
            conn.commit()
            return ResetResult(path, cleared, preserved, backup)
        finally:
            if conn.in_transaction:
                conn.rollback()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="服务实际使用的 SQLite 数据库路径")
    parser.add_argument("--apply", action="store_true", help="确认清空全部业务数据（必须先停止机器人服务）")
    parser.add_argument("--backup-dir", type=Path, help="备份目录，默认数据库旁的 backups/")
    args = parser.parse_args(argv)
    try:
        result = reset_business_data(args.db, apply=args.apply, backup_dir=args.backup_dir)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"未完成清理：{error}", file=sys.stderr)
        return 1
    print(f"数据库：{result.database}")
    print("已清理：" if args.apply else "预览，将清理：")
    for table, count in result.cleared.items():
        print(f"  {table}: {count}")
    print(f"共 {sum(result.cleared.values())} 条业务记录。")
    print("保留：.env 中的超管与机器人参数、群/老师权限、周赛开关、负责老师、标识名称、通知设置、历史编号。")
    if result.backup:
        print(f"完整备份：{result.backup}")
        print("清理完成，可以启动机器人服务。")
    else:
        print("未修改数据。核对路径和数量后，先停止服务，再加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
