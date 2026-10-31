"""配置体检：检查 .env 是否齐全，只报告键名与状态，绝不打印密钥明文。

用法：
    python scripts/check_env.py

退出码：0 全部必填项已配；1 存在必填项缺失。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


def _mask(value: str) -> str:
    """密钥类只显示是否已填，不显示内容。"""
    return "已填写" if value else "—— 未填 ——"


def _show(value: str) -> str:
    """非密钥类可以显示，过长截断。"""
    if not value:
        return "—— 未填 ——"
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def main() -> int:
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

    # 已废弃的配置项：留着不会报错，但代码不会读取
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
    rows = (
        ("ACM_ONEBOT_WS_URL", config.ONEBOT_WS_URL, _show),
        ("ACM_LLM_API_KEY", config.LLM_API_KEY, _mask),
    )
    for name, value, fmt in rows:
        ok = bool(value)
        if not ok:
            missing.append(name)
        print(f"  {'✓' if ok else '✗'} {name:32s} {fmt(value)}")

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
        print(f"  · {name:32s} {_show(value)}")

    print()
    if missing:
        print("结论：还差 " + "、".join(missing) + "，补齐后即可启动。")
        print("提示：编辑 .env 后重新运行本脚本确认。")
        return 1
    print("结论：必填项齐全，可以启动：python main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
