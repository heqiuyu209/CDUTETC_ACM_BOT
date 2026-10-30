"""本机管理员导出指定群的征集明细；不连接 QQ，不修改数据库。"""

import argparse
import csv
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


def _csv_text(value: str) -> str:
    """将用户填写的公式前缀作为文本导出。"""
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def export_collection(db_path: str | Path, group_id: str, collection_id: int,
                      output_path: str | Path) -> int:
    """只读导出当前报名者；归属不匹配或内容损坏时不创建输出文件。"""
    if not isinstance(group_id, str) or not group_id.strip():
        raise ValueError("群标识不能为空")
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        collection = conn.execute(
            "SELECT fields_json FROM collections WHERE id = ? AND group_id = ?",
            (collection_id, group_id.strip()),
        ).fetchone()
        if collection is None:
            raise ValueError("征集不存在或不属于指定群")
        rows = conn.execute(
            "SELECT member_id, data_json FROM collection_responses"
            " WHERE collection_id = ? AND group_id = ? AND status = 'registered' ORDER BY id",
            (collection_id, group_id.strip()),
        ).fetchall()
    finally:
        conn.close()
    try:
        declared = json.loads(collection[0] or "[]")
        if not isinstance(declared, list) or not all(isinstance(key, str) for key in declared):
            raise ValueError
        keys = list(dict.fromkeys(declared))
        data_rows = []
        for member_id, raw in rows:
            if not isinstance(member_id, str):
                raise ValueError
            data = json.loads(raw or "{}")
            if not isinstance(data, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in data.items()
            ):
                raise ValueError
            for key in data:
                if key not in keys:
                    keys.append(key)
            data_rows.append((member_id, data))
    except (ValueError, TypeError):
        raise ValueError("征集登记内容格式错误，未导出") from None

    output = Path(output_path)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["member_id", *(_csv_text("data." + key) for key in keys)])
            for member_id, data in data_rows:
                writer.writerow([_csv_text(member_id), *(_csv_text(data.get(key, "")) for key in keys)])
    except Exception:
        output.unlink()
        raise
    return len(data_rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.DB_PATH, help="本机 SQLite 数据库路径")
    parser.add_argument("--group-id", required=True, help="征集所属群 openid")
    parser.add_argument("--collection-id", required=True, type=int, help="征集 id")
    parser.add_argument("--output", required=True, help="新的本地 CSV 文件路径")
    args = parser.parse_args(argv)
    try:
        count = export_collection(args.db, args.group_id, args.collection_id, args.output)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"导出失败：{error}", file=sys.stderr)
        return 1
    print(f"已导出 {count} 条报名到本地文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
