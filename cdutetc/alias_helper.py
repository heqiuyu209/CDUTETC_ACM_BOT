"""openid 展示层统一入口：能解析就显示人话，解析不了才显示短哈希。

官方 QQ 协议只给单向哈希的 group_openid / member_openid，真实群号与 QQ 号
拿不回来。管理员在群内「认领」后写入 entity_aliases，本模块负责把 openid
渲染成「备注名 (群号/QQ)」；未认领时降级成带省略号的短哈希，保证任何提示里
都不会出现一长串看不懂的字符串。
"""

import sqlite3

from cdutetc import database as db

# 短哈希保留的头尾长度，与 database.describe_account 的观感保持一致。
HEAD_CHARS = 6
TAIL_CHARS = 4


def short_openid(openid: str) -> str:
    """把 openid 压缩成「前6位...后4位」；短标识原样返回。"""
    text = (openid or "").strip()
    if len(text) > HEAD_CHARS + TAIL_CHARS + 1:
        return f"{text[:HEAD_CHARS]}...{text[-TAIL_CHARS:]}"
    return text


def _alias(entity_type: str, openid: str) -> tuple[str, str]:
    """返回 (备注名, 真实群号/QQ号)；未认领时退回旧命名表的名字。"""
    openid = (openid or "").strip()
    if not openid:
        return "", ""
    try:
        alias = db.get_entity_alias(entity_type, openid) or {}
        name = (alias.get("display_name") or "").strip()
        numeric = (alias.get("numeric_id") or "").strip()
        if not name:
            # 兼容只走过 /命名群 /命名人 的历史数据
            name = (db.get_account_label(entity_type, openid) or "").strip()
    except sqlite3.Error:
        # 老库还没建表（init_db 之前）或库被占用：降级成短哈希，绝不抛给调用方
        return "", ""
    return name, numeric


def format_group_name(group_id: str) -> str:
    """群的可读名称：「备注名 (群号)」/「备注名」/「未命名群[短哈希]」。"""
    group_id = (group_id or "").strip()
    if not group_id:
        return "未命名群"
    name, numeric = _alias("group", group_id)
    if name:
        return f"{name} ({numeric})" if numeric else name
    return f"未命名群[{short_openid(group_id)}]"


def format_user_name(user_id: str) -> str:
    """成员的可读名称：「姓名 (QQ: 号)」/「姓名」/「成员[短哈希]」。"""
    user_id = (user_id or "").strip()
    if not user_id:
        return "成员"
    name, numeric = _alias("user", user_id)
    if name:
        return f"{name} (QQ: {numeric})" if numeric else name
    return f"成员[{short_openid(user_id)}]"
