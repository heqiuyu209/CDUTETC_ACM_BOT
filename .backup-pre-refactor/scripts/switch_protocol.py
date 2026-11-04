"""一键切换接入协议：在 OneBot（NapCat）与 QQ 官方机器人之间切换。

用法：
    python scripts/switch_protocol.py qqbot --appid 123456 --secret abcdef
    python scripts/switch_protocol.py onebot
    python scripts/switch_protocol.py --show        # 只看当前协议

行为：
- 自动备份为 .env.bak.<时间戳>，可随时回滚；
- 只改动协议相关键，其余配置（白名单、模型、提醒…）原样保留；
- 不改群/人白名单——切到 qqbot 后它们需要换成 openid，脚本会提醒。
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SystemExit(f"读取 {path} 失败：{error}")


def _get(lines: list[str], key: str) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip()
    return ""


def _set(lines: list[str], updates: dict[str, str]) -> tuple[list[str], list[str]]:
    """就地更新已有键，返回（新行列表, 被追加的键）。"""
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    if pending:
        out.append("")
        out.append("# ===== 切换协议时自动补充 =====")
        for key, value in pending.items():
            out.append(f"{key}={value}")
    return out, list(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description="切换接入协议")
    parser.add_argument("protocol", nargs="?", choices=("onebot", "qqbot"),
                        help="目标协议")
    parser.add_argument("--appid", default="", help="官方机器人 AppID")
    parser.add_argument("--secret", default="", help="官方机器人 AppSecret")
    parser.add_argument("--show", action="store_true", help="只显示当前协议")
    args = parser.parse_args()

    if not ENV_FILE.exists():
        raise SystemExit(f"找不到 {ENV_FILE}，请先运行 python main.py 生成")

    lines = _read_lines(ENV_FILE)
    current = _get(lines, "ACM_BOT_PROTOCOL") or "onebot"

    if args.show or not args.protocol:
        print(f"配置文件：{ENV_FILE}")
        print(f"当前协议：{current}")
        print(f"AppID　 ：{_get(lines, 'ACM_QQBOT_APPID') or '—— 未填 ——'}")
        secret = _get(lines, "ACM_QQBOT_SECRET")
        print(f"AppSecret：{'已填写（%d 字符）' % len(secret) if secret else '—— 未填 ——'}")
        if args.show:
            return 0

    target = args.protocol
    updates = {"ACM_BOT_PROTOCOL": target}
    if target == "qqbot":
        if args.appid:
            updates["ACM_QQBOT_APPID"] = args.appid
        if args.secret:
            updates["ACM_QQBOT_SECRET"] = args.secret

    new_lines, appended = _set(lines, updates)

    # 需要真正写入时才备份
    changed = [k for k in updates if _get(lines, k) != updates[k]]
    if not changed:
        print(f"已经处于 {target} 协议，无需修改。")
        return 0

    backup = ENV_FILE.with_name(f".env.bak.{datetime.now():%Y%m%d%H%M%S}")
    shutil.copyfile(ENV_FILE, backup)
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    print(f"已切换为：{target}")
    print(f"已备份到：{backup.name}")
    for key in changed:
        print(f"  · {key} = {updates[key] if 'SECRET' not in key else '***'}")

    if target == "qqbot":
        appid = _get(new_lines, "ACM_QQBOT_APPID")
        secret = _get(new_lines, "ACM_QQBOT_SECRET")
        if not appid or not secret:
            print("\n⚠ 还缺 AppID / AppSecret，补齐后才能启动：")
            print("  python scripts/switch_protocol.py qqbot --appid xxx --secret yyy")
            return 1
        print("\n接下来必须做（官方机器人拿不到 QQ 号，只有 openid）：")
        print("  1. 临时设置 ACM_DEBUG_LOG_IDENTITY=1，并清空 ACM_ALLOWED_GROUPS")
        print("  2. 启动后到群里 @ 机器人 发一句 /帮助")
        print("  3. 从日志抄下 group_id 和 user_id，填进 ACM_ALLOWED_GROUPS / ACM_SUPER_ADMIN")
        print("  4. 把 ACM_DEBUG_LOG_IDENTITY 改回 0")
        print("  提示：超管要先私聊机器人发一次 /帮助，之后审批私聊才送得到。")
    else:
        print("\n注意：切回 OneBot 后，白名单要填回 QQ 群号 / QQ 号（不是 openid）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
