"""配置体检：检查 .env 是否齐全，只报告键名与状态，绝不打印密钥明文。

用法：
    python scripts/check_env.py              # 体检本项目的 .env
    python scripts/check_env.py --file PATH  # 体检任意 .env（比如云端拷下来的）

--file 模式不会加载该文件、不改动任何环境，只做「键是否存在、是否已填」的比对，
因此可以放心把输出贴给别人；含 KEY/SECRET/TOKEN 的值一律不显示。

退出码：0 必填项齐全；1 存在必填项缺失。
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 这些键名里带敏感词，只报告「是否已填」，不显示内容
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "APPID")


def _is_secret(name: str) -> bool:
    upper = name.upper()
    return any(hint in upper for hint in _SECRET_HINTS)


def _show(name: str, value: str) -> str:
    if not value:
        return "—— 未填 ——"
    if _is_secret(name):
        return f"已填写（{len(value)} 字符）"
    return value if len(value) <= 60 else value[:57] + "..."


def _parse(path: Path) -> dict[str, str]:
    """解析 .env 为字典；不依赖 python-dotenv，避免影响当前进程环境。"""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"读取失败：{error}")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _template_keys() -> list[str]:
    template = ROOT / ".env.example"
    if not template.exists():
        return []
    return list(_parse(template))


def report_file(path: Path) -> int:
    """体检任意 env 文件：只输出键名/状态，输出可直接发给他人。"""
    values = _parse(path)
    template = _template_keys()
    print("=" * 58)
    print("CDUTETC ACM 事务助手 · 配置体检（离线文件）")
    print("=" * 58)
    print(f"配置文件：{path}")
    print(f"键数量　：{len(values)}")

    deprecated = ("ACM_APPID", "ACM_APP_SECRET", "ACM_PUBLISHER_WHITELIST")
    leftovers = [name for name in deprecated if name in values]
    if leftovers:
        print(f"可删除　：{'、'.join(leftovers)}（旧版遗留，已不再读取）")

    missing_keys = [key for key in template if key not in values]
    extra_keys = [key for key in values if template and key not in template]

    protocol = values.get("ACM_BOT_PROTOCOL", "onebot").strip().lower()
    if protocol == "qqbot":
        required = ("ACM_QQBOT_APPID", "ACM_QQBOT_SECRET", "ACM_LLM_API_KEY")
    else:
        required = ("ACM_ONEBOT_WS_URL", "ACM_LLM_API_KEY")

    print(f"\n[必填] 当前协议 {protocol}")
    missing = []
    for name in required:
        value = values.get(name, "")
        if not value:
            missing.append(name)
        print(f"  {'✓' if value else '✗'} {name:32s} {_show(name, value)}")

    print("\n[建议填写] 留空则对应功能不可用")
    for name in ("ACM_ALLOWED_GROUPS", "ACM_SUPER_ADMIN", "ACM_TEACHER_WHITELIST"):
        value = values.get(name, "")
        print(f"  {'✓' if value else '·'} {name:32s} {_show(name, value)}")

    if missing_keys:
        print(f"\n[缺少的新配置项] {len(missing_keys)} 个（拉取新代码后会自动补齐）")
        for name in missing_keys:
            print(f"  - {name}")
    else:
        print("\n[缺少的新配置项] 无，已与 .env.example 对齐")

    if extra_keys:
        print(f"\n[模板中没有的键] {len(extra_keys)} 个（可能是旧版或手写）")
        for name in extra_keys:
            print(f"  ? {name}")

    print()
    if missing:
        print("结论：还差 " + "、".join(missing) + "。")
        return 1
    print("结论：必填项齐全。")
    return 0


def report_project() -> int:
    from cdutetc import config

    print("=" * 58)
    print("CDUTETC ACM 事务助手 · 配置体检")
    print("=" * 58)
    print(f"配置文件：{config.ENV_FILE}")
    if config.ENV_CREATED:
        print("状态　　：本次已按 .env.example 新建，请填写下面的必填项")
    elif not Path(config.ENV_FILE).exists():
        print("状态　　：⚠ 不存在，将使用系统环境变量或代码默认值")
    else:
        print("状态　　：已存在")
    if config.ENV_ADDED_KEYS:
        print(f"已补齐　：本次自动补入 {len(config.ENV_ADDED_KEYS)} 个新增配置项")
    print(f"接入协议：{config.BOT_PROTOCOL}")

    deprecated = ("ACM_APPID", "ACM_APP_SECRET", "ACM_PUBLISHER_WHITELIST")
    if Path(config.ENV_FILE).exists():
        try:
            text = Path(config.ENV_FILE).read_text(encoding="utf-8")
        except OSError:
            text = ""
        leftovers = [
            name for name in deprecated
            if f"\n{name}=" in f"\n{text}" or text.startswith(f"{name}=")
        ]
        if leftovers:
            print(f"可删除　：{('、'.join(leftovers))}（旧版遗留下来的配置，已不再读取）")

    print("\n[必填] 缺失将无法启动")
    missing = []
    for name, value in config.REQUIRED_CONFIG.items():
        if not value:
            missing.append(name)
        print(f"  {'✓' if value else '✗'} {name:32s} {_show(name, value)}")

    print("\n[建议填写] 留空则对应功能不可用")
    for name, value in (
        ("ACM_ALLOWED_GROUPS", sorted(config.ENV_ALLOWED_GROUPS)),
        ("ACM_SUPER_ADMIN", sorted(config.SUPER_ADMIN)),
        ("ACM_TEACHER_WHITELIST", sorted(config.ENV_TEACHER_WHITELIST)),
    ):
        shown = "、".join(value) if value else "—— 未填 ——"
        print(f"  {'✓' if value else '·'} {name:32s} {shown}")

    print("\n[运行参数] 仅供参考，可保持默认")
    for name, value in (
        ("ACM_LLM_MODEL", config.LLM_MODEL),
        ("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", config.GROUP_ACTIVE_MESSAGES_ENABLED),
        ("ACM_PUBLISH_CONFIRM_ENABLED", config.PUBLISH_CONFIRM_ENABLED),
        ("ACM_PUBLISH_SCOPE_TO_SOURCE", config.PUBLISH_SCOPE_TO_SOURCE),
        ("ACM_QA_MEMBER_KB_ONLY", config.QA_MEMBER_KB_ONLY),
        ("ACM_REMIND_OFFSET_HOURS", config.REMIND_OFFSET_HOURS),
        ("ACM_WEEKLY_CONTEST_ENABLED", config.WEEKLY_CONTEST_ENABLED),
        ("ACM_WEEKLY_CONTEST_GROUPS", sorted(config.WEEKLY_CONTEST_GROUPS) or "全部群"),
        ("ACM_DB_PATH", config.DB_PATH),
    ):
        print(f"  · {name:32s} {_show(name, str(value))}")

    print()
    if missing:
        print("结论：还差 " + "、".join(missing) + "，补齐后即可启动。")
        print("提示：编辑 .env 后重新运行本脚本确认。")
        return 1
    print("结论：必填项齐全，可以启动：python main.py")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="配置体检（不会打印任何密钥明文）")
    parser.add_argument(
        "--file", type=Path, default=None,
        help="体检任意 .env 文件（例如从云端复制下来的），不加载到当前环境",
    )
    args = parser.parse_args()
    if args.file is not None:
        return report_file(args.file)
    return report_project()


if __name__ == "__main__":
    raise SystemExit(main())
