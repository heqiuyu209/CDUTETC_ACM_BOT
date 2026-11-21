"""问答命令：/问答增删查、/提问、自动应答与知识库兜底、非命令闲聊应答。"""

import logging
import re
from cdutetc import config, content_filter, llm, database as db, faq as faq_search
from cdutetc.utils import can_manage_group

from cdutetc.handlers import _shared as _shared

logger = logging.getLogger(__name__)


def cmd_faq_list(group_id: str, sender: str, args: str) -> list[dict]:
    # FAQ 列表含内部维护口径，仅老师/超管可见，避免普通成员看到管理端内容。
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看 FAQ 列表"}]
    keyword = args.strip() or None
    faqs = db.list_faq(group_id, keyword)
    if not faqs:
        return [{"group_id": group_id, "text": "本群暂无 FAQ" + ("（含关键词）" if keyword else "")}]
    lines = ["本群 FAQ："]
    for f in faqs:
        lines.append(f"#{f['id']} Q：{f['question']}")
        lines.append(f"   A：{f['answer']}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_faq_add(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可添加 FAQ"}]
    if "=" not in args:
        return [{"group_id": group_id, "text": "用法：/FAQ添加 <问题> = <答案>"}]
    q, a = args.split("=", 1)
    try:
        fid = db.add_faq(group_id, q.strip(), a.strip(), source="老师手工添加", operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    return [{"group_id": group_id, "text": f"已添加 FAQ #{fid}"}]


def cmd_faq_delete(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可删除 FAQ"}]
    try:
        fid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/FAQ删除 <id>"}]
    ok = db.delete_faq(group_id, fid, sender)
    if not ok:
        return [{"group_id": group_id, "text": f"FAQ #{fid} 不存在或不属于本群"}]
    return [{"group_id": group_id, "text": f"已删除 FAQ #{fid}"}]


def _faq_cite(faq: dict) -> str:
    """FAQ 直答正文：默认附来源编号，方便老师核对、成员追问时对号。"""
    answer = str(faq.get("answer") or "").strip()
    if not config.QA_FAQ_CITE_ENABLED:
        return answer
    return f"{answer}\n（依据 FAQ #{faq['id']}）"


_PENDING_LIST_LIMIT = 10
# 私聊确认语：刻意避开发布审批的「同意/通过/拒绝/驳回 + 编号」，两套审批互不抢占。
_PENDING_DECISION_RE = re.compile(
    r"^(?P<verb>faq确认|faq驳回|确认)\s*(?P<ids>全部|全选|所有|all|\d[\d\s,，、]*)$", re.I,
)
_ALL_PENDING_WORDS = frozenset({"全部", "全选", "所有", "all"})


def _parse_pending_ids(raw: str) -> list[int] | None:
    """解析编号串：「12 13」「#12」「12,13」→ [12, 13]；「全部」→ None；空 → []。"""
    text = (raw or "").strip()
    if not text:
        return []
    if text.lower() in _ALL_PENDING_WORDS:
        return None
    return [int(token) for token in re.findall(r"\d+", text)]


def cmd_faq_pending_list(group_id: str, sender: str, args: str) -> list[dict]:
    """查看本群待确认的问答草稿（老师规则通知自动提炼，未被确认前不参与回答）。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看待确认问答"}]
    items = db.list_pending_faq(group_id)
    if not items:
        return [{"group_id": group_id,
                 "text": "本群没有待确认的问答草稿。\n"
                         "老师在群里发队规/制度类通知时，会自动提炼成草稿放在这里。"}]
    lines = [f"待确认问答草稿（{len(items)} 条）："]
    for item in items[:_PENDING_LIST_LIMIT]:
        lines.append(f"#{item['id']} Q：{item['question']}")
        lines.append(f"   A：{item['answer']}")
    if len(items) > _PENDING_LIST_LIMIT:
        rest = len(items) - _PENDING_LIST_LIMIT
        lines.append(f"……另有 {rest} 条，可先按编号处理前 {_PENDING_LIST_LIMIT} 条。")
    lines.append("")
    lines.append("通过：/FAQ确认 <编号>（多个编号空格分隔，或 /FAQ确认 全部）")
    lines.append("丢弃：/FAQ驳回 <编号>（或 /FAQ驳回 全部）")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def _decide_pending(group_id: str, operator: str, args: str, *, confirm: bool) -> list[dict]:
    """群内批量确认/驳回本群草稿；逐条给出结果，不做“全部成功”式的模糊回执。"""
    command = "FAQ确认" if confirm else "FAQ驳回"
    ids = _parse_pending_ids(args)
    if ids == []:
        return [{"group_id": group_id,
                 "text": f"用法：/{command} <编号>，可写多个编号或「全部」。\n"
                         "先用 /FAQ待确认 查看待处理草稿。"}]
    if ids is None:
        ids = [item["id"] for item in db.list_pending_faq(group_id)]
        if not ids:
            return [{"group_id": group_id, "text": "本群没有待确认的问答草稿。"}]
    done: list[int] = []
    missing: list[int] = []
    for fid in ids:
        if db.get_pending_faq(group_id, fid) is None:
            missing.append(fid)
            continue
        ok = (db.confirm_faq(group_id, fid, operator=operator) if confirm
              else db.reject_faq(group_id, fid, operator=operator))
        (done if ok else missing).append(fid)
    lines: list[str] = []
    if done:
        lines.append(("已确认 " if confirm else "已驳回 ")
                     + f"{len(done)} 条：" + "、".join(f"#{i}" for i in done))
        lines.append("已生效，之后的同类提问会直接由机器人回答。" if confirm
                     else "这些草稿不会再被采用。")
    if missing:
        lines.append("未处理：" + "、".join(f"#{i}" for i in missing)
                     + "（不存在、不属于本群或不是待确认状态）")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_faq_confirm(group_id: str, sender: str, args: str) -> list[dict]:
    """确认草稿生效（确认后同类提问即可直接作答）。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可确认问答草稿"}]
    return _decide_pending(group_id, sender, args, confirm=True)


def cmd_faq_reject(group_id: str, sender: str, args: str) -> list[dict]:
    """驳回草稿（保留审计，但不再参与任何召回）。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可驳回问答草稿"}]
    return _decide_pending(group_id, sender, args, confirm=False)


def handle_pending_decision(sender: str, text: str, *, allow_any: bool = False) -> list[str] | None:
    """私聊回复「确认 12」/「驳回全部」：处理本人提炼、尚未生效的问答草稿。

    归属校验按草稿 source 里的提交人做，默认只认自己的草稿——老师之间不互相覆盖
    对方刚发的队规；超管（``allow_any``）可代为处理任意草稿。

    Returns:
        回执行列表；文本不符合确认/驳回语法时返回 None，交由调用方按普通私聊处理。
    """
    match = _PENDING_DECISION_RE.match((text or "").strip())
    if not match:
        return None
    verb = match.group("verb").lower()
    confirm = verb != "faq驳回"
    raw = match.group("ids").strip()
    if raw.lower() in _ALL_PENDING_WORDS:
        owner = "" if allow_any else sender
        ids = [item["id"] for item in db.list_pending_faq(owner=owner)]
        if not ids:
            return ["你没有待确认的问答草稿。" if not allow_any else "当前没有待确认的问答草稿。"]
    else:
        ids = [int(token) for token in re.findall(r"\d+", raw)]
    own = db.teacher_source(sender)
    done: list[int] = []
    foreign: list[int] = []
    absent: list[int] = []
    for fid in ids:
        record = db.get_faq_record(fid)
        if record is None or record.get("status") != db.FAQ_PENDING:
            absent.append(fid)
            continue
        if not allow_any and record.get("source") != own:
            foreign.append(fid)
            continue
        ok = (db.confirm_faq(record["group_id"], fid, operator=sender) if confirm
              else db.reject_faq(record["group_id"], fid, operator=sender))
        (done if ok else absent).append(fid)
    lines: list[str] = []
    if done:
        lines.append(("已确认 " if confirm else "已驳回 ")
                     + f"{len(done)} 条：" + "、".join(f"#{i}" for i in done))
        lines.append("已在群里生效，之后同类提问我会直接答。" if confirm
                     else "这些草稿已丢弃，不会再被引用。")
    if foreign:
        lines.append("不是你的草稿，未处理：" + "、".join(f"#{i}" for i in foreign))
    if absent:
        lines.append("不存在或已处理：" + "、".join(f"#{i}" for i in absent))
    return lines or ["没有可处理的内容。"]


def cmd_ask(group_id: str, sender: str, args: str) -> list[dict]:
    """记录未答问题。若 FAQ 已命中（上层先查过），不会走到这里。"""
    question = args.strip()
    if not question:
        return [{"group_id": group_id, "text": "用法：/提问 <问题>"}]
    qid = db.add_unanswered_question(group_id, question, sender)
    return [{"group_id": group_id, "text": f"已记录问题 #{qid}，老师看到后会回答（回答后会加入 FAQ）"}]


def cmd_question_list(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看未答问题"}]
    items = db.list_unanswered(group_id, status="open")
    if not items:
        return [{"group_id": group_id, "text": "暂无未答问题"}]
    lines = ["未答问题："]
    for q in items:
        lines.append(f"#{q['id']} {q['question']}（提问者：{q['asker'] or '未知'}）")
    lines.append("直接回答：/回答 <编号> = <答案>")
    lines.append("（回答会自动存入本群 FAQ，之后同类问题由机器人直接答）")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_answer_question(group_id: str, sender: str, args: str) -> list[dict]:
    """老师按编号回答未答问题，并沉淀为 FAQ（一条命令完成闭环）。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可回答问题"}]
    match = _shared._ANSWER_RE.match(args or "")
    if not match:
        return [{"group_id": group_id,
                 "text": "用法：/回答 <编号> = <答案>\n例：/回答 3 = 本周日 19:00 在机房"}]
    qid = int(match.group(1))
    answer = match.group(2).strip()
    if not answer:
        return [{"group_id": group_id, "text": "答案不能为空"}]
    try:
        result = db.answer_unanswered_question(group_id, qid, answer, operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    if result is None:
        return [{"group_id": group_id,
                 "text": f"问题 #{qid} 不存在、不属于本群或已处理过"}]
    return [{"group_id": group_id,
             "text": f"已回答 #{qid} 并存入 FAQ #{result['faq_id']}。\n"
                     f"问题：{result['question']}\n"
                     "之后同类提问会直接由机器人回答。"}]


async def match_faq(group_id: str, text: str, *, exact_only: bool = False) -> dict | None:
    """单次读取本群 FAQ；高置信本地匹配，模糊时按配置选择模型消歧。"""
    return await faq_search.find(group_id, text, db.list_faq(group_id), exact_only=exact_only)


def _business_name_match(text: str, title: str) -> tuple[int, bool]:
    """完整名称优先；名称末尾数字必须作为完整编号匹配。"""
    title = title.strip()
    if not title:
        return 0, True
    numbered = re.fullmatch(r"(.*?\D)(\d+)", title)
    if numbered:
        mentioned = re.search(re.escape(numbered[1]) + r"(\d+)", text, re.I)
        if mentioned and mentioned[1] != numbered[2]:
            return 0, False
    pattern = ((r"(?<!\d)" if title[0].isdigit() else "") + re.escape(title)
               + (r"(?!\d)" if title[-1].isdigit() else ""))
    return (len(title) if re.search(pattern, text, re.I) else 0), True


_TEAM_DEFINITION = re.compile(
    r"(?:请问|想了解一下|想了解)?\s*(?:(?:ACM\s*)?集训队(?:是(?:什么|啥)|干什么|做什么)(?:的|用的)?"
    r"|(?:什么|啥)是\s*(?:ACM\s*)?集训队)[呀啊呢吗?？。！!\s]*", re.I,
)
_DEFINITION_QUESTION = re.compile(r"(?:什么|啥)是|(?:是什么|是啥|的定义|什么意思)[呀啊呢吗?？。！!\s]*$", re.I)
_ARRANGEMENT_TOPIC = re.compile(r"比赛|竞赛|校赛|周赛|选拔|课程|训练|集训|上课|课表|讲座|安排")


def _business_query_fields(text: str) -> tuple[str, ...]:
    """只提取用户实际所问字段，定义和闲聊不作为安排查询。"""
    fields = []
    if re.search(r"报名.*(?:时间|何时|什么时候|几点|几号)|截止", text):
        fields.append("deadline")
    if (re.search(r"开赛|开打|开始|比赛时间|课程时间|上课时间|周几", text)
            or "deadline" not in fields and re.search(r"时间|什么时候|几点|几号|何时", text)):
        fields.append("time")
    if re.search(r"地点|位置|所在地|在哪|哪里|哪儿|何处|教室|机房", text):
        fields.append("location")
    if re.search(r"形式|线上|线下", text):
        fields.append("mode")
    if re.search(r"官网|网址|链接", text):
        fields.append("link")
    if re.search(r"要求|设备|电脑|软件|材料", text):
        fields.append("requirement")
    return tuple(fields)


def _is_arrangement_query(text: str) -> bool:
    if _TEAM_DEFINITION.fullmatch(text.strip()) or (
        _DEFINITION_QUESTION.search(text) and not _business_query_fields(text)
    ):
        return False
    if not content_filter.is_information_request(text) and not re.search(r"线上还是线下|线下还是线上|哪(?:间|里|儿|个)|所在地", text):
        return False
    return bool(_business_query_fields(text) or (
        _ARRANGEMENT_TOPIC.search(text) and re.search(r"安排|课表|怎么排|有|讲|上|查|看", text)
    ))


def _business_field_value(item: dict, kind: str, field: str) -> str:
    keys = {
        "deadline": ("reg_deadline",), "location": ("location",),
        "mode": ("mode",), "link": ("source_link",),
        "requirement": ("device_req", "needs_laptop", "software"),
        "time": (("contest_time",) if kind == "competition" else ("weekday", "start_time", "end_time")),
    }
    return "|".join(str(item.get(key) or "").strip() for key in keys[field])


def _select_business_fact(text: str, competitions: list[dict], sessions: list[dict]
                          ) -> tuple[str, dict | None, str | None]:
    """明确安排查询才召回；同分记录在所问字段不同则询问对象。"""
    from cdutetc import qa_context

    if not _is_arrangement_query(text):
        return "", None, None
    fields = _business_query_fields(text)
    weekday = re.search(r"(?:周|星期|礼拜)[一二三四五六日天]", text)
    requested_weekday = qa_context._training_weekday(weekday[0]) if weekday else None
    contest_query = bool(re.search(r"比赛|竞赛|校赛|周赛", text))
    course_query = bool(re.search(r"课程|训练|集训|上课|课表|讲座", text))
    ranked = []
    for kind, records, title_key, extra_key in (
        ("competition", competitions, "name", "contest_time"),
        ("training_session", sessions, "title", "location"),
    ):
        for item in records:
            title = item.get(title_key) or ""
            exact_length, compatible = _business_name_match(text, title)
            if not compatible:
                continue
            if not exact_length and not _ARRANGEMENT_TOPIC.search(text):
                continue
            if not exact_length and ((kind == "competition" and course_query and not contest_query)
                                     or (kind == "training_session" and contest_query and not course_query)):
                continue
            if requested_weekday is not None:
                if kind == "training_session":
                    actual_weekday = qa_context._training_weekday(item.get("weekday"))
                else:
                    moment = db._delivery_parse_time(item.get("contest_time") or "")
                    actual_weekday = moment.weekday() if moment else None
                if actual_weekday is not None and actual_weekday != requested_weekday:
                    continue
            score = content_filter.record_match_score(text, title, item.get(extra_key) or "")
            if exact_length or score >= config.QA_KB_MATCH_MIN_SCORE:
                ranked.append((exact_length, 1.0 if exact_length else score, kind, item))
    if not ranked:
        return "", None, None
    best_rank = max((row[0], row[1]) for row in ranked)
    top = [row for row in ranked if (row[0], row[1]) == best_rank]
    different = tuple(field for field in fields if len({
        _business_field_value(row[3], row[2], field) for row in top
    }) > 1)
    if len(top) > 1 and (different or not fields):
        labels = {"deadline": "报名截止", "time": "时间", "location": "地点", "mode": "形式",
                  "link": "官网", "requirement": "要求"}
        subject = "、".join(labels[field] for field in different)
        lines = ([f"{subject}：匹配到多项{subject}不同的安排，请明确要查询哪一项。"] if subject
                 else ["匹配到多项安排，请明确要查询哪一项。"])
        for _length, _score, kind, item in top[:5]:
            label = "比赛" if kind == "competition" else "课程"
            title = item.get("name" if kind == "competition" else "title") or "未命名安排"
            when = (item.get("contest_time") if kind == "competition" else
                    " ".join(str(item.get(key) or "") for key in ("weekday", "start_time")).strip())
            lines.append(f"· {label} {item['id']}：{title}" + (f"｜{when}" if when else ""))
        return "", None, "\n".join(lines)
    return top[0][2], top[0][3], None


async def _answer_from_knowledge_base(text: str, group_id: str) -> str | None:
    """只查库内已录入内容作答，不调用模型（普通成员问答路径）。

    FAQ 已由入口查询；此处依次尝试已录入比赛、本群课程、学校安排。
    命中返回整理好的纯事实文本；全部未命中返回 None（由调用方转人工）。

    设计要点：比赛名、时间、地点等全部原样取自数据库，不做任何推测补全，
    因此不会出现模型编造或“我没有数据库权限”这类管理端话术。
    """
    if not _is_arrangement_query(text):
        return None
    threshold = config.QA_KB_MATCH_MIN_SCORE
    kind, selected, clarification = _select_business_fact(
        text, db.list_question_contests(group_id, limit=50),
        db.list_training_sessions(group_id, active_only=True),
    )
    if clarification:
        return clarification

    # 1) 已录入比赛：Dice 相似度 + 标题关键词命中取最高（口语问法也能召回）
    best = selected if kind == "competition" else None
    if best is not None:
        name = best.get('name') or '未命名比赛'
        fields = (("报名截止", "reg_deadline"), ("比赛时间", "contest_time"),
                  ("形式", "mode"), ("地点", "location"), ("官网", "source_link"))
        wanted = set()
        if re.search(r"截止|报名.*(时间|何时|什么时候|几点|几号)", text):
            wanted.add("reg_deadline")
        if re.search(r"开赛|开打|开始|比赛时间", text) or (
            "报名" not in text and "截止" not in text and re.search(r"什么时候|几点|几号|何时", text)
        ):
            wanted.add("contest_time")
        if re.search(r"地点|在哪|哪里|哪儿|教室|机房", text):
            wanted.add("location")
        if re.search(r"形式|线上|线下", text):
            wanted.add("mode")
        if re.search(r"官网|网址|链接", text):
            wanted.add("source_link")
        if wanted:
            fields = tuple((label, key) for label, key in fields if key in wanted)
        lines = [f"「{name}」目前登记的安排："]
        for label, key in fields:
            value = (best.get(key) or "").strip()
            if value:
                lines.append(f"{label}：{value}")
            elif wanted:
                lines.append(f"{label}：还没登记，暂时无法确定")
        return "\n".join(lines)

    # 2) 本群课程安排
    best_t = selected if kind == "training_session" else None
    if best_t is not None:
        from cdutetc import qa_context

        lines = [f"【{best_t.get('title') or '集训课'}】"]
        weekday_index = qa_context._training_weekday(best_t.get("weekday"))
        start, end = qa_context._normalized_clocks(best_t.get("start_time"), best_t.get("end_time"))
        if weekday_index is not None and start is not None:
            weekday = qa_context._WEEKDAY_LABELS[weekday_index]
            span = start.strftime("%H:%M") + (f"-{end:%H:%M}" if end else "")
            date = qa_context._single_occurrence_date(best_t)
            lines.append(f"时间：{date + ' ' if date else ''}{weekday} {span}")
        elif re.search(r"时间|什么时候|几点|周几|何时", text):
            lines.append("时间：还没登记，暂时无法确定")
        if best_t.get("location"):
            lines.append(f"地点：{best_t['location']}")
        if best_t.get("needs_laptop"):
            lines.append("需要带电脑")
        if best_t.get("software"):
            lines.append(f"软件：{best_t['software']}")
        if best_t.get("device_req"):
            lines.append(f"设备：{best_t['device_req']}")
        return "\n".join(lines)

    # 3) 学校安排（仅可共享给学生的条目）
    best_e, best_e_score = None, 0.0
    for event in db.list_org_events(status="confirmed", shareable_only=True):
        score = content_filter.record_match_score(
            text, event.get("title") or "", event.get("location") or "",
        )
        if score > best_e_score:
            best_e, best_e_score = event, score
    if best_e is not None and best_e_score >= threshold:
        lines = [f"【{best_e.get('title') or '学校安排'}】"]
        for label, key in (("类型", "event_type"), ("报名截止", "reg_deadline"),
                           ("时间", "contest_time"), ("地点", "location"),
                           ("负责老师", "teacher"), ("备注", "note")):
            value = (best_e.get(key) or "").strip()
            if value:
                lines.append(f"{label}：{value}")
        return "\n".join(lines)

    return None


# 机器人能力说明：由固定话术回答，不走模型，避免模型臆造「做不到」。
# 面向群成员公开，只讲能做什么，不涉及实现细节。
CAPABILITY_TEXT = """【我能做的事】
- 查信息：比赛、课程、学校安排、常见问题都可以问我
- 自动提醒：比赛报名截止、开赛，征集截止，课程开课，按管理员设置提醒群里
- 报名：群里发起的征集，可以直接用 /报名 参加
- 发布：老师把比赛信息发给我，我整理后由超管确认，再按设置通知群里
- 闲聊：@我 随便聊两句也行，接梗、吐槽 WA 都可以

有问题直接 @我 就行。"""

TEAM_DEFINITION_TEXT = (
    "ACM 集训队通常是一起练习算法和程序设计、准备 ICPC、CCPC 等竞赛的学生团队。"
    "本群具体训练安排和加入方式以老师录入的资料为准；我可以帮你查询课程、比赛和报名信息。"
)


# 非问句、FAQ 未命中时的本地寒暄兜底。拟人聊天开启后由 persona_chat 接管改写，
# 这里提成常量，既避免字面量散落，也让上层能识别「这只是兜底而非真答」。
SOCIAL_FALLBACK_TEXT = "我在呢～有想问的直接说就好。"


def is_social_fallback(replies: list[dict] | None) -> bool:
    """判断回复是否只是寒暄兜底（空回复或仅兜底一句），可被拟人聊天接管。"""
    texts = [str(reply.get("text") or "").strip() for reply in (replies or [])
             if reply.get("text")]
    return not texts or all(text == SOCIAL_FALLBACK_TEXT for text in texts)


async def handle_non_command_message(text: str, group_id: str, sender: str,
                                     staff: bool = False) -> list[dict]:
    """非命令消息问答。

    闲聊本地回复，普通陈述不自动登记；非问句仅允许完整命中 FAQ 词条。
    staff=False（普通成员）：真正的队务提问只答库内资料，未命中才收集为未答问题。
    staff=True（老师/超管）：提问按 FAQ → 算法拦截 → 模型问答处理。

    text/group_id/sender 均由调用方校验完毕；本函数不做权限判断。
    """
    if content_filter.is_social_chat(text):
        if config.QA_SOCIAL_CHAT_ENABLED:
            return [{"group_id": group_id, "text": await llm.answer_social_chat(text)}]
        return []
    if re.fullmatch(r"(?:请问|查一下|看看)?(?:(?:本群|我们群|群里|这周|本周|近期|最近|现在|今天|接下来)的?){0,2}"
                    r"(?:有什么|有哪些|有没有|有啥)(?:比赛|竞赛)(?:安排)?[吗呢啊呀?？。！!\s]*", text.strip()):
        from cdutetc import group_admin
        items = group_admin.contests(group_id, this_week=bool(re.search(r"这周|本周", text)))
        lines = ["本群比赛：" if items else "本群暂无符合条件的已登记比赛。"]
        lines.extend(f"· {item['name']}｜{item.get('contest_time') or '时间待定'}" for item in items[:10])
        return [{"group_id": group_id, "text": "\n".join(lines)}]
    is_request = content_filter.is_information_request(text)
    faq = await match_faq(group_id, text, exact_only=not is_request)
    if faq is not None:
        return [{"group_id": group_id, "text": _faq_cite(faq)}]
    if _TEAM_DEFINITION.fullmatch(text.strip()):
        return [{"group_id": group_id, "text": TEAM_DEFINITION_TEXT}]
    if not is_request:
        return ([{"group_id": group_id, "text": SOCIAL_FALLBACK_TEXT}]
                if config.QA_SOCIAL_CHAT_ENABLED else [])
    # 能力类提问用固定话术，避免模型臆造「我不能自动提醒」这类错误结论
    if content_filter.is_capability_query(text):
        return [{"group_id": group_id, "text": CAPABILITY_TEXT}]
    # 算法话题（题解/思路/概念科普）一律不调模型，FAQ 已优先命中，避免浪费 token
    if config.QA_PROBLEM_GATE_ENABLED and content_filter.is_algorithm_query(text):
        return [{"group_id": group_id, "text": config.QA_PROBLEM_REPLY}]

    # 老师与成员都先答本群已登记事实，命中不必花模型 Token。
    answer = await _answer_from_knowledge_base(text, group_id)
    if answer is not None:
        return [{"group_id": group_id, "text": answer}]
    # 普通成员：未命中转人工，全程不产生模型调用
    if not staff and config.QA_MEMBER_KB_ONLY:
        # 可选：先用动态上下文模型作答（事实覆盖不到才转工单），默认关闭保持零 token
        if config.QA_MEMBER_CONTEXT_ANSWER and config.LLM_API_KEY:
            contextual = await _answer_from_context(text, group_id)
            if contextual is not None:
                return [{"group_id": group_id, "text": contextual}]
        qid = db.add_unanswered_question(group_id, text, sender)
        return [{"group_id": group_id,
                 "text": f"{config.QA_MEMBER_NO_MATCH_REPLY}（问题编号 #{qid}）"}]

    answer = await _answer_question_with_context(text, group_id)
    if answer.startswith("模型暂时不可用"):
        qid = db.add_unanswered_question(group_id, text, sender)
        return [{"group_id": group_id,
                 "text": f"模型暂时用不了，已先记为问题 #{qid}，老师看到后会回复。"}]
    if llm.UNKNOWN_MARKER in answer:
        qid = db.add_unanswered_question(group_id, text, sender)
        return [{"group_id": group_id,
                 "text": f"这条我暂时没查到，已记录为问题 #{qid}；"
                         "补一份 FAQ（/FAQ添加）或课程/征集后我就能直接答。"}]
    return [{"group_id": group_id, "text": answer}]


async def _answer_question_with_context(text: str, group_id: str) -> str:
    """带上当前群教务事实的模型问答；模型异常按「模型暂时不可用」返回。"""
    from cdutetc import qa_context

    group_context = qa_context.build_group_qa_context(group_id, question=text) if config.QA_CONTEXT_ENABLED else ""
    try:
        return await llm.answer_question(
            text, db.list_question_contests(group_id), group_context=group_context,
        )
    except Exception:
        return "模型暂时不可用，请稍后再试~"


async def _answer_from_context(text: str, group_id: str) -> str | None:
    """上下文模型作答；模型不可用或判定未收录时返回 None，交由调用方转工单。"""
    answer = await _answer_question_with_context(text, group_id)
    if not answer or answer.startswith("模型暂时不可用") or llm.UNKNOWN_MARKER in answer:
        return None
    return answer
