"""发布消息文案与格式化：通知摘要/详情、发布时间清洗、回执与审核卡片、可见范围扩展。"""

from __future__ import annotations

import logging

from cdutetc import main as _main

logger = logging.getLogger(__name__)


def _notification_summary(sent: int, total: int) -> str:
    if total == 0:
        return "群通知未发送：没有已授权的目标群，请核对比赛通知范围和群授权。"
    if not _main.config.GROUP_ACTIVE_MESSAGES_ENABLED and sent == 0:
        return "群通知未发送：主动群消息未启用，请联系管理员。"
    if sent == total:
        return f"群通知已发送：{sent}/{total} 个目标群。"
    if sent == 0:
        return f"群通知未确认发送：0/{total} 个目标群，请管理员检查平台权限或限额。"
    return (
        f"群通知已发送：{sent}/{total} 个目标群；"
        f"其余 {total - sent} 个群未确认发送，请管理员检查平台权限或限额。"
    )


def _notification_details(results: list[tuple[str, _main.DeliveryResult]]) -> str:
    summary = _notification_summary(sum(result.ok for _, result in results), len(results))
    lines = [summary]
    for gid, result in results[:10]:
        try:
            label = _main.db.describe_account("group", gid)
        except _main.sqlite3.Error:
            label = gid if len(gid) <= 14 else f"{gid[:8]}…{gid[-4:]}"
        status = "平台已接受" if result.ok else result.user_hint
        lines.append(f"{label}：{status}")
    if len(results) > 10:
        lines.append(f"另有 {len(results) - 10} 个群，已计入上方汇总；失败详情见日志。")
    return "\n".join(lines)


def _build_publish_notice(info: dict, cid: int) -> str:
    """构造发往各群的新比赛公告（字段全部取自已确认的录入内容）。"""
    lines = [f"【新比赛发布】#{cid} {info.get('name') or '未知'}"]
    if info.get("edition"):
        lines.insert(1, f"届次：{info['edition']}")
    for label, key in (("报名开始", "official_reg_start"),
                       ("报名截止", "official_reg_deadline"),
                       ("比赛时间", "official_time"),
                       ("赛道", "tracks"),
                       ("设备要求", "device_req")):
        value = (info.get(key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    if (info.get("summary") or "").strip():
        lines.append(f"简介：{info['summary'].strip()}")
    if (info.get("official_site") or "").strip():
        lines.append(f"官网：{info['official_site'].strip()}")
    lines.append("请及时查看并提前做好准备~")
    return "\n".join(lines)


_TIME_FIELD_LABELS = (
    ("official_reg_start", "报名开始"),
    ("official_reg_deadline", "报名截止"),
    ("official_time", "比赛时间"),
)


def _sanitize_publish_times(info: dict) -> tuple[dict, list[str]]:
    """把解析不了的时间降级为空串，并把原文记进简介，绝不因此拒绝保存。

    校赛常见「下学期开学初」这类表述，数据库只接受 YYYY-MM-DD HH:MM。
    强行入库会在超管同意时报错，因此在这里先降级：时间留空（=待定），
    原文写进简介，回执里也提示出来，等日期确定后再 /修正 补。
    """
    cleaned = dict(info)
    notes: list[str] = []
    for key, label in _TIME_FIELD_LABELS:
        raw = (cleaned.get(key) or "").strip()
        if not raw:
            continue
        normalized = _main.normalize_time(raw)
        if normalized is None:
            notes.append(f"{label}待定：{raw}")
            cleaned[key] = ""
        else:
            cleaned[key] = normalized
    if notes:
        existing = (cleaned.get("summary") or "").strip()
        merged = "；".join(notes)
        cleaned["summary"] = f"{existing}\n{merged}".strip() if existing else merged
    return cleaned, notes


def _format_remind_offsets() -> str:
    """开赛提醒以专用分钟配置为准，不能显示旧版24h/1h节点。"""
    return "、".join(f"{m} 分钟" for m in _main.config.CONTEST_REMIND_OFFSETS_MINUTES)


# 发布回执里展示的字段顺序（名称单独处理）
_RECEIPT_FIELDS = (
    ("届次", "edition"),
    ("报名开始", "official_reg_start"),
    ("报名截止", "official_reg_deadline"),
    ("比赛时间", "official_time"),
    ("赛道", "tracks"),
    ("设备要求", "device_req"),
    ("官网", "official_site"),
    ("简介", "summary"),
)


def _build_publish_receipt(info: dict, *, status: str, ref: int) -> str:
    """构造发布回执：清楚告诉发布者「存了什么、缺什么、下一步是什么」。

    status 取值「待超管确认」或「已入库」；ref 为申请编号或比赛编号。
    """
    filled, missing = [], []
    for label, key in _RECEIPT_FIELDS:
        value = (info.get(key) or "").strip()
        if value:
            filled.append(f"{label}：{value}")
        else:
            missing.append(label)

    lines = [f"✅ 已保存：{info.get('name') or '未命名比赛'}"]
    lines.append(f"编号：#{ref}（{status}）")
    lines.extend(filled)
    if missing:
        lines.append(f"暂无信息：{'、'.join(missing)}（不影响使用，后续可用 /修正 补充）")

    remind_time = (info.get("official_time") or "").strip()
    if remind_time:
        if status == "待超管确认":
            lines.append(f"提醒：超管同意后安排开始前 {_format_remind_offsets()}的未来节点，已错过的节点不补发")
        else:
            lines.append(f"提醒：按开始前 {_format_remind_offsets()}的未来有效节点发到群里")
    else:
        lines.append("提醒：时间未定，补上比赛时间后才会自动提醒")
    return "\n".join(lines)


def _runtime_teacher_record(account_id: str) -> dict | None:
    """在动态老师名单里按 openid 精确匹配，命中才返回记录。

    只做精确相等，绝不做前缀/模糊匹配——展示身份宁可落到兜底，也不能张冠李戴。
    """
    if not account_id:
        return None
    try:
        teachers = _main.db.list_runtime_teachers() or []
    except (_main.sqlite3.Error, ValueError):
        return None
    for record in teachers:
        if str(record.get("user_id") or "").strip() == account_id:
            return record
    return None


def _format_submitter(submitted_by: str) -> str:
    """实时解析比赛发布人的展示文案（备注名 → 超管 → 老师 → 兜底）。

    严格只认「当前这条消息触发者」的真实账号标识（群聊 member_id / 私聊 uid），
    严禁回退成群默认负责老师：身份漂移会让待确认卡片把超管记成老师。
    """
    account_id = (submitted_by or "").strip()
    if not account_id:
        return "未知成员（无账号标识）"
    short = account_id[:8]

    try:
        label_name = (_main.db.get_account_label("user", account_id) or "").strip()
    except (_main.sqlite3.Error, ValueError):
        label_name = ""
    try:
        alias = _main.db.get_entity_alias("user", account_id) or {}
    except (_main.sqlite3.Error, ValueError):
        alias = {}
    alias_name = str(alias.get("display_name") or "").strip()
    alias_qq = str(alias.get("numeric_id") or "").strip()

    # 1) 超管身份锁定：有备注展示备注，没有就展示超级管理员；不读老师名单覆盖
    if _main.is_super_admin(account_id):
        if label_name:
            return f"{label_name}（超管）"
        return f"超级管理员（ID: {short}...）"

    # 2) 已绑定老师：runtime_teachers 里存在精确 openid 记录
    teacher = _runtime_teacher_record(account_id)
    if teacher is not None:
        teacher_name = (
            label_name
            or alias_name
            or str(teacher.get("note") or "").strip()
        )
        teacher_qq = alias_qq
        if not teacher_name:
            teacher_name = f"老师（ID: {short}...）"
        if teacher_qq:
            return f"{teacher_name}（老师, QQ: {teacher_qq}）"
        return f"{teacher_name}（老师）"

    # 3) 兜底：既不是超管也不在老师名单；有备注名/已绑 QQ 时仍展示，避免信息丢失
    display_name = label_name or alias_name
    if display_name and alias_qq:
        return f"{display_name}（QQ: {alias_qq}）"
    if display_name:
        return f"{display_name}（ID: {short}...）"
    return f"未知成员（ID: {short}...）"


def _build_publish_review(pending: dict) -> str:
    """构造发给超管的待确认申请正文（含全部已整理字段与查重提示）。"""
    info = _main.json.loads(pending.get("fields_json") or "{}")
    lines = [f"【待确认发布】#{pending['id']}", f"比赛：{info.get('name') or '未知'}"]
    for label, key in (("届次", "edition"),
                       ("报名开始", "official_reg_start"),
                       ("报名截止", "official_reg_deadline"),
                       ("比赛时间", "official_time"),
                       ("赛道", "tracks"),
                       ("设备要求", "device_req"),
                       ("简介", "summary"),
                       ("官网", "official_site")):
        value = (info.get(key) or "").strip()
        lines.append(f"{label}：{value or '未提供'}")
    if (info.get("official_time") or "").strip():
        lines.append(f"提醒计划：同意后安排开始前 {_format_remind_offsets()}的未来节点，已错过的不补发")
    lines.append(f"来源群：{_main.format_group_name(pending.get('group_id') or '')}")
    lines.append(f"提交人：{_format_submitter(pending.get('submitted_by') or '')}")
    if pending.get("duplicate_note"):
        lines.append(f"⚠ 查重提示：{pending['duplicate_note']}")
    else:
        lines.append("查重：未发现名称+届次+比赛时间完全一致的记录")
    lines.append("")
    lines.append(f"回复「同意 {pending['id']}」入库并通知群，或「拒绝 {pending['id']}」丢弃。")
    return "\n".join(lines)


_PUBLISH_DECISION_RE = _main.re.compile(
    r"^(?P<action>同意|通过|批准|拒绝|驳回|approve|reject|ok|y|n)"
    r"\s*(?P<pid>\d+)?\s*(?P<note>.*)$",
    _main.re.IGNORECASE,
)


def _apply_publish_scope(cid: int, group_id: str) -> None:
    """新发布的比赛默认只通知「发布它的群」。

    这样集训队群发的比赛不会打扰新生群，反之亦然。
    想通知多个群时用 /通知群 <id> 群号,群号 扩展。
    ACM_PUBLISH_SCOPE_TO_SOURCE=0 可恢复「发布即通知全部群」的旧行为。
    """
    if not _main.config.PUBLISH_SCOPE_TO_SOURCE or not group_id:
        return
    try:
        _main.db.set_competition_notify_groups(cid, {group_id}, operator="publish:scope")
    except (_main.sqlite3.Error, ValueError) as e:
        logger.warning("发布通知范围设置失败: %s", e)


def _extend_scope_with_group(cid: int, group_id: str) -> bool:
    """把某个群并入比赛通知范围。返回是否真的新增了群。

    范围原本为空（=全部允许群）时不改动，返回 False。
    """
    if not group_id:
        return False
    try:
        scope = _main.db.get_competition_notify_groups(cid)
    except (_main.sqlite3.Error, ValueError):
        return False
    if not scope or group_id in scope:
        return False
    scope.add(group_id)
    try:
        _main.db.set_competition_notify_groups(cid, scope, operator="publish:extend")
    except (_main.sqlite3.Error, ValueError) as e:
        logger.warning("扩展通知范围失败: %s", e)
        return False
    return True



