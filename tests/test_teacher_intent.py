"""老师群内自然语言通知：三层管线（门禁 / 0 Token 预过滤 / 分类落库）。

除模型调用与链接抓取外全部走真实实现：真 sqlite 临时库、真正则、真提醒推算。
模型一律用脚本化替身，保证「同一句话 → 同一分类 → 同一落库结果」可复现。
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, fetcher, llm, reminder, teacher_intent
from cdutetc import database as db

TZ = ZoneInfo("Asia/Shanghai")
GID = "123456"
TEACHER = "654321"
MEMBER = "999999"
# 固定「今天」为周三 10:00：周期课/单次课/比赛的提醒节点才有确定答案。
NOW = datetime(2026, 11, 18, 10, 0)
NOW_AWARE = NOW.replace(tzinfo=TZ)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = str(tmp_path / "teacher_intent.sqlite3")
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path, raising=False)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GID}, raising=False)
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {TEACHER}, raising=False)
    monkeypatch.setattr(config, "SUPER_ADMIN", {"root"}, raising=False)
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True, raising=False)
    db.init_db()
    return GID


@pytest.fixture()
def model(monkeypatch):
    """脚本化模型：给出 dict 即返回该 JSON，并可断言调用次数与入参。"""
    calls: list[tuple[str, str]] = []

    async def fake_chat_once(system_prompt, user_prompt, json_mode=False, max_tokens=0):
        calls.append((system_prompt, user_prompt))
        payload = model.reply
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload, ensure_ascii=False)

    model.reply = {}
    model.calls = calls
    monkeypatch.setattr(llm, "_chat_once", fake_chat_once)
    return model


def _course_payload(**override):
    payload = {
        "intent": "RECURRING_COURSE", "confidence": "high", "title": "图论基础",
        "weekday": "周三", "start_time": "19:00", "end_time": "21:00", "location": "B302",
    }
    payload.update(override)
    return payload


# ---------------------------------------------------------------------
# 第一层：身份门禁与 0 Token 预过滤
# ---------------------------------------------------------------------

async def test_member_message_never_enters_the_pipeline(env, model):
    """队员发言直接放行给后续模块：返回 None，且一次模型调用都不产生。"""
    result = await teacher_intent.handle_message(
        "明天下午三点在机房集训", group_id=GID, sender_id=MEMBER, msg_id="m1", now=NOW)
    assert result is None
    assert model.calls == []
    assert db.list_training_sessions(GID) == []


async def test_teacher_short_smalltalk_is_filtered_without_tokens(env, model):
    """老师短闲聊（清理后不足 5 字）在本地就被丢掉，绝不调模型。"""
    result = await teacher_intent.handle_message(
        "好的👌收到", group_id=GID, sender_id=TEACHER, msg_id="m2", now=NOW)
    assert result["action"] == "ignored"
    assert result["reply"] == ""
    assert model.calls == []


async def test_teacher_long_message_without_anchor_is_filtered(env, model):
    """有长度但没有时间/事件锚点，同样不花 token。"""
    assert teacher_intent.should_call_llm("大家这周都很努力辛苦了") is False
    result = await teacher_intent.handle_message(
        "大家这周都很努力辛苦了，继续保持", group_id=GID, sender_id=TEACHER,
        msg_id="m3", now=NOW)
    assert result["action"] == "ignored"
    assert model.calls == []


def test_anchor_rules_cover_time_and_event_phrases():
    """两类锚点各自独立生效：任一命中即可进入模型。"""
    assert teacher_intent.should_call_llm("明天下午集训") is True          # 时间锚点
    assert teacher_intent.should_call_llm("机房换到实训楼了") is True      # 事件锚点
    assert teacher_intent.should_call_llm("今晚") is False                # 长度不足


# ---------------------------------------------------------------------
# 第三层（high）：落库 + 提醒排队 + 被动回执
# ---------------------------------------------------------------------

async def test_recurring_course_is_saved_with_receipt_and_reminder_nodes(env, model):
    """周期课 high：写入 weekday 型课程，回执极简，24h 提醒节点可被扫描命中。"""
    model.reply = _course_payload()
    result = await teacher_intent.handle_message(
        "每周三晚上7点在B302讲图论", group_id=GID, sender_id=TEACHER, msg_id="c1", now=NOW)

    assert result["action"] == "saved"
    assert result["intent"] == teacher_intent.INTENT_RECURRING_COURSE
    assert result["reply"].startswith("📌 已自动登记课程【图论基础】")
    assert "周三 19:00" in result["reply"] and "B302" in result["reply"]

    course = db.list_training_sessions(GID)[0]
    # 原文没给结束时间，不能将模型补出来的21点存成事实。
    assert (course["weekday"], course["start_time"], course["end_time"]) == ("周三", "19:00", "")
    assert course["location"] == "B302" and course["status"] == "confirmed"
    # 周期课不写死生效期，否则下个月自动失效。
    assert not course.get("effective_start")

    # 今天 19:00 开课，距现在 9 小时：应命中 24 小时提醒节点。
    nodes = reminder.build_training_reminders(db.list_training_sessions(GID), NOW_AWARE)
    assert [node["offset_hours"] for node in nodes] == [24.0]
    assert nodes[0]["target_str"] == "2026-11-18 19:00"


async def test_one_off_session_is_marked_effective_for_that_day(env, model):
    """单次集训 high：靠 effective_start/end 把生效期夹在当天，并命中 1 小时节点。"""
    model.reply = {
        "intent": "ONE_OFF_SESSION", "confidence": "high", "title": "DP 专题",
        "date": "2026-11-18", "start_time": "10:40", "end_time": "12:00", "location": "机房",
    }
    result = await teacher_intent.handle_message(
        "今天10点40在机房讲DP专题", group_id=GID, sender_id=TEACHER, msg_id="o1", now=NOW)

    assert result["action"] == "saved" and "已自动登记集训" in result["reply"]
    session = db.list_training_sessions(GID, active_only=False)[0]
    assert session["effective_start"] == "2026-11-18 00:00"
    assert session["effective_end"] == "2026-11-18 23:59"
    assert (session["weekday"], session["start_time"]) == ("周三", "10:40")

    nodes = reminder.build_training_reminders([session], NOW_AWARE)
    assert [node["offset_hours"] for node in nodes] == [1.0]


async def test_contest_with_url_is_enriched_and_scheduled(env, model, monkeypatch):
    """比赛 high：链接正文补地点，入库后绑定本群并生成开赛前 15 分钟节点。"""
    async def fake_fetch(url, *args, **kwargs):
        assert url == "https://example.com/contest/1"
        return "CDUTETC 新生赛，地点：实训楼A301，报名截止。"
    monkeypatch.setattr(fetcher, "fetch_text", fake_fetch)

    model.reply = {
        "intent": "CONTEST", "confidence": "high", "title": "新生选拔赛",
        "date": "2026-11-18", "start_time": "10:15",
        "url": "https://example.com/contest/1",
    }
    result = await teacher_intent.handle_message(
        "今天10点15新生选拔赛，链接 https://example.com/contest/1，地点看通知",
        group_id=GID, sender_id=TEACHER, msg_id="k1", now=NOW)

    assert result["action"] == "saved" and "已自动登记比赛" in result["reply"]
    contest = db.get_competition(result["object_id"])
    assert contest["official_time"] == "2026-11-18 10:15"
    assert contest["official_site"] == "https://example.com/contest/1"
    assert db.get_competition_target_groups(contest["id"]) == {GID}

    upcoming = reminder.build_upcoming_reminders(GID, NOW_AWARE, days=7)
    contest_nodes = [item for item in upcoming if item["source"] == "contest"]
    assert contest_nodes, upcoming
    minutes = sorted((datetime(2026, 11, 18, 10, 15, tzinfo=TZ) - node["trigger"]).total_seconds() / 60
                     for node in contest_nodes)
    assert 15.0 in minutes


async def test_update_reschedules_course_and_drops_stale_nodes(env, model):
    """改期 high：课程时间被改，旧 pending 提醒节点作废，下次实例按新时间重排。"""
    db.create_training_session(GID, {
        "title": "常规集训", "weekday": "周三", "start_time": "19:00", "location": "B302",
        "status": "confirmed",
    }, operator=TEACHER)
    course_id = db.list_training_sessions(GID)[0]["id"]
    db.ensure_delivery(GID, "training_session", course_id, "start_time",
                       "2026-11-18 19:00", 24)
    assert db.list_deliveries(GID, statuses=("pending",)), "前置条件：应存在旧节点"

    model.reply = {
        "intent": "UPDATE_CANCEL", "confidence": "high", "target_id": course_id,
        "weekday": "周四", "start_time": "19:00", "location": "B305",
    }
    result = await teacher_intent.handle_message(
        "周三的课改到周四晚上7点，地点B305", group_id=GID, sender_id=TEACHER,
        msg_id="u1", now=NOW)

    assert result["action"] == "saved" and "已更新课程" in result["reply"]
    course = db.get_group_course(GID, course_id)
    assert (course["weekday"], course["start_time"], course["location"]) == ("周四", "19:00", "B305")
    # 旧节点必须清空，否则「待发清单」会留着已经不存在的时间点。
    assert db.list_deliveries(GID, statuses=("pending",)) == []

    occurrences = db.training_occurrence_times(course, NOW_AWARE, 48)
    assert datetime(2026, 11, 19, 19, 0, tzinfo=TZ) in occurrences


async def test_cancel_course_keeps_data_but_stops_reminders(env, model):
    """取消 high：课程置为非生效并清空待发节点，回执说明已取消。"""
    db.create_training_session(GID, {
        "title": "常规集训", "weekday": "周三", "start_time": "19:00", "status": "confirmed",
    }, operator=TEACHER)
    course_id = db.list_training_sessions(GID)[0]["id"]
    db.ensure_delivery(GID, "training_session", course_id, "start_time", "2026-11-18 19:00", 1)

    model.reply = {"intent": "UPDATE_CANCEL", "confidence": "high",
                   "target_id": course_id, "cancel": True}
    result = await teacher_intent.handle_message(
        "这周三的课取消", group_id=GID, sender_id=TEACHER, msg_id="x1", now=NOW)

    assert result["action"] == "saved" and "已取消课程" in result["reply"]
    assert db.list_training_sessions(GID) == []
    assert db.list_deliveries(GID, statuses=("pending",)) == []


# ---------------------------------------------------------------------
# 第三层（medium / ignore）：待补全与静默
# ---------------------------------------------------------------------

async def test_vague_time_goes_pending_and_pings_teacher_privately(env, model):
    """时间模糊：不猜时间，进 pending 队列并私聊催老师与超管补全。"""
    model.reply = {
        "intent": "ONE_OFF_SESSION", "confidence": "medium", "title": "DP 专题",
        "missing": ["date", "start_time"],
    }
    result = await teacher_intent.handle_message(
        "下周找个晚上给大家讲DP", group_id=GID, sender_id=TEACHER, msg_id="p1", now=NOW)

    assert result["action"] == "pending"
    assert "还缺" in result["reply"] and "私聊" in result["reply"]
    records = db.list_teacher_intent_pending(GID)
    assert len(records) == 1
    assert records[0]["intent"] == "ONE_OFF_SESSION"
    assert set(records[0]["missing"]) == {"date", "start_time"}
    receivers = {uid for uid, _ in result["notify_private"]}
    assert receivers == {TEACHER, "root"}
    # 模糊通知绝不落库：待确认之前不产生任何课程。
    assert db.list_training_sessions(GID) == []


async def test_confidence_high_but_missing_slots_falls_back_to_pending(env, model):
    """模型自称 high 却缺开始时间时，仍按待补全处理，不写半条课程。"""
    model.reply = _course_payload(start_time="")
    result = await teacher_intent.handle_message(
        "每周三在B302上课", group_id=GID, sender_id=TEACHER, msg_id="p2", now=NOW)

    assert result["action"] == "pending"
    assert db.list_training_sessions(GID) == []


async def test_chat_ignore_is_silent(env, model):
    """带时间词的闲聊判定为 CHAT_IGNORE：不落库、不回群消息。"""
    model.reply = {"intent": "CHAT_IGNORE", "confidence": "high"}
    result = await teacher_intent.handle_message(
        "昨晚CF打得不错，下周继续保持", group_id=GID, sender_id=TEACHER,
        msg_id="i1", now=NOW)

    assert result["action"] == "ignored"
    assert result["reply"] == "" and result["notify_private"] == []
    assert db.list_training_sessions(GID) == []
    assert db.list_teacher_intent_pending(GID) == []


async def test_model_failure_degrades_to_silence(env, model):
    """模型超时/异常时宁可不动作：静默忽略，绝不往群里发东西。"""
    model.reply = TimeoutError("boom")
    result = await teacher_intent.handle_message(
        "明天下午三点机房集训", group_id=GID, sender_id=TEACHER, msg_id="f1", now=NOW)

    assert result["action"] == "ignored" and result["reply"] == ""
    assert db.list_training_sessions(GID) == []


async def test_invalid_json_is_ignored(env, model, monkeypatch):
    """模型返回非 JSON 时同样静默，不抛异常给消息回调。"""
    async def fake_chat_once(*args, **kwargs):
        return "这不是 JSON"
    monkeypatch.setattr(llm, "_chat_once", fake_chat_once)

    result = await teacher_intent.handle_message(
        "明天下午三点机房集训", group_id=GID, sender_id=TEACHER, msg_id="j1", now=NOW)
    assert result["action"] == "ignored"


async def test_context_carries_beijing_time_and_compact_indexes(env, model):
    """送进模型的上下文必须含北京时间与本群课程/比赛精简索引。"""
    db.create_training_session(GID, {
        "title": "常规集训", "weekday": "周三", "start_time": "19:00", "end_time": "21:00",
        "location": "B302", "status": "confirmed",
    }, operator=TEACHER)
    db.create_competition({"name": "新生赛", "official_time": "2026-11-20 19:00"},
                          status="confirmed", operator=TEACHER)

    model.reply = {"intent": "CHAT_IGNORE", "confidence": "high"}
    await teacher_intent.handle_message(
        "明晚的课正常进行", group_id=GID, sender_id=TEACHER, msg_id="t1", now=NOW)

    system_prompt, user_prompt = model.calls[0]
    assert "2026-11-18 10:00" in system_prompt and "周三" in system_prompt
    payload = json.loads(user_prompt)
    assert payload["message"] == "明晚的课正常进行"
    assert payload["courses"][0]["title"] == "常规集训"
    assert payload["courses"][0]["type"] == "training_session"
    assert payload["courses"][0]["date"] == "2026-11-18"
    assert payload["courses"][0]["start_time"] == "19:00"
    assert payload["courses"][0]["end_time"] == "21:00"
    assert payload["courses"][0]["location"] == "B302"
    assert set(payload["courses"][0]) == {"id", "type", "title", "date", "weekday", "start_time", "end_time", "location"}
    assert payload["contests"][0]["name"] == "新生赛"
    assert payload["contests"][0]["type"] == "competition"
    assert payload["contests"][0]["time"] == "2026-11-20 19:00"
    # 类型区分撞号，实际日期/时间/地点供补充字段复用，不带整条记录。
    assert set(payload["contests"][0]) == {"id", "type", "name", "time", "location"}


def test_teacher_gate_accepts_whitelist_registered_teacher_and_super_admin(env, monkeypatch):
    """身份门禁：白名单、群登记老师、超管放行；其他人（含空 ID）一律拒绝。"""
    assert teacher_intent.is_teacher_speaker(GID, TEACHER) is True
    assert teacher_intent.is_teacher_speaker(GID, "root") is True
    assert teacher_intent.is_teacher_speaker(GID, MEMBER) is False
    assert teacher_intent.is_teacher_speaker(GID, "") is False

    monkeypatch.setattr(config, "TEACHER_WHITELIST", set(), raising=False)
    monkeypatch.setattr(db, "get_group_teachers", lambda gid: ["teacher-2"])
    assert teacher_intent.is_teacher_speaker(GID, "teacher-2") is False
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher-2"})
    assert teacher_intent.is_teacher_speaker(GID, "teacher-2") is True
    assert teacher_intent.is_teacher_speaker(GID, MEMBER) is False


def test_receipt_is_single_line_and_omits_empty_location():
    """回执必须极简：一行、无地点时不出现空尾段。"""
    line = teacher_intent._course_receipt({
        "title": "图论基础", "weekday": "周三", "start_time": "19:00", "end_time": "21:00",
    })
    assert line == "📌 已自动登记课程【图论基础】｜周三 19:00-21:00"
    assert "\n" not in line


def test_short_message_length_boundary():
    """长度阈值边界：剔除 @ 与表情后仍有 5 字，才可能送进模型。"""
    assert teacher_intent.normalize_text("<@!abc> 明天训练 😀") == "明天训练"
    assert teacher_intent.should_call_llm("<@!abc> 讲课") is False        # 2 字
    assert teacher_intent.should_call_llm("<@!abc> 明天讲课") is False    # 4 字，差一个字符
    assert teacher_intent.should_call_llm("<@!abc> 明晚在机房讲课") is True


def test_fetched_summary_location_fallback():
    """正文里只有「地点：」标签时才补地点，抽不到就留空。"""
    assert teacher_intent._location_from_summary("比赛通知 地点：实训楼B302 时间待定") == "实训楼B302"
    assert teacher_intent._location_from_summary("比赛通知 时间待定") == ""


def test_url_sanitizer_rejects_credentials_and_non_http():
    """带账号密码或非 http(s) 的链接一律不抓、不入库。"""
    assert teacher_intent._sanitize_url("https://example.com/a") == "https://example.com/a"
    assert teacher_intent._sanitize_url("https://user:pass@example.com/a") == ""
    assert teacher_intent._sanitize_url("file:///etc/passwd") == ""
    assert teacher_intent._sanitize_url("javascript:alert(1)") == ""


# ---------------------------------------------------------------------
# 长哲理夹安排：分句定位 / 放宽截断 / 多意图拆分 / 哲理过滤
# ---------------------------------------------------------------------

def test_candidate_windows_locates_arrangement_with_context():
    """候选句连同前后相邻句一起送模，抽槽原文只取含安排的那句。"""
    text = "人生如逆水行舟不进则退。每周三晚上7点在B302讲图论。有些路要自己走。"
    windows = teacher_intent._candidate_windows(text)
    assert len(windows) == 1
    focus, source = windows[0]
    assert source.startswith("每周三") and "讲图论" in source
    # 相邻哲理句进入送模正文，给足语境。
    assert "人生如逆水行舟" in focus and "有些路要自己走" in focus
    # 全无锚点时回退整段，行为与旧版一致。
    plain = "人生如逆水行舟不进则退。"
    assert teacher_intent._candidate_windows(plain) == [(plain, plain)]


async def test_arrangement_embedded_mid_philosophy_is_saved(env, model):
    """长哲理中段夹一句安排：只送候选窗口、落库课程，哲理句不误入库。"""
    model.reply = _course_payload()
    text = (
        "人生如逆水行舟不进则退，有些路注定要一个人走。"
        "每周三晚上7点在B302讲图论，大家务必坚持。"
        "成长往往发生在不经意之间，愿各位珍惜这段时光。"
    )
    result = await teacher_intent.handle_message(
        text, group_id=GID, sender_id=TEACHER, msg_id="ph1", now=NOW)

    assert result["action"] == "saved"
    assert "图论基础" in result["reply"]
    assert len(db.list_training_sessions(GID)) == 1
    # 单个候选窗口 → 一次送模；送模正文含安排句而非被哲理淹没。
    assert len(model.calls) == 1
    assert "讲图论" in json.loads(model.calls[0][1])["message"]


async def test_arrangement_after_400_chars_is_not_truncated(env, model):
    """安排在 400 字之后：放宽截断 + 分句定位后仍能识别并落库。"""
    prelude = "人生如逆水行舟不进则退，有些路注定要一个人走。" * 20
    assert len(prelude) > 400
    model.reply = _course_payload()
    text = prelude + "每周三晚上7点在B302讲图论。"

    result = await teacher_intent.handle_message(
        text, group_id=GID, sender_id=TEACHER, msg_id="ph2", now=NOW)

    assert result["action"] == "saved"
    assert len(db.list_training_sessions(GID)) == 1
    sent = json.loads(model.calls[0][1])["message"]
    assert "讲图论" in sent and len(sent) < len(text)  # 分句定位，未把整段哲理塞进模型


async def test_no_event_anchor_arrangement_with_time_is_recognized(env, model):
    """无事件锚点但有明确时间的安排：靠时间信号定位并落库。"""
    model.reply = {
        "intent": "ONE_OFF_SESSION", "confidence": "high", "title": "操场集合",
        "date": "2026-11-19", "start_time": "20:00",
    }
    result = await teacher_intent.handle_message(
        "明天晚上8点在操场集合。", group_id=GID, sender_id=TEACHER,
        msg_id="ph3", now=NOW)

    assert result["action"] == "saved"
    sessions = db.list_training_sessions(GID, active_only=False)
    assert len(sessions) == 1 and sessions[0]["start_time"] == "20:00"


def test_parse_result_accepts_json_array():
    """模型输出多意图 JSON 数组时解析为列表，非法项被过滤。"""
    raw = json.dumps([
        {"intent": "RECURRING_COURSE", "confidence": "high", "title": "图论"},
        {"intent": "CHAT_IGNORE", "confidence": "high"},
        {"title": "缺 intent"},
    ], ensure_ascii=False)
    parsed = teacher_intent.parse_result(raw)
    assert isinstance(parsed, list) and len(parsed) == 2
    assert parsed[0]["title"] == "图论" and parsed[1]["intent"] == "CHAT_IGNORE"


async def test_json_array_multiple_intents_are_applied_and_merged(env, model):
    """单条消息多意图数组：逐条落 pending 并合并回执与私聊。"""
    model.reply = [
        {"intent": "ONE_OFF_SESSION", "confidence": "medium", "title": "DP 专题",
         "missing": ["date", "start_time"]},
        {"intent": "ONE_OFF_SESSION", "confidence": "medium", "title": "图论 专题",
         "missing": ["date", "start_time"]},
    ]
    result = await teacher_intent.handle_message(
        "下周找时间讲DP和图论", group_id=GID, sender_id=TEACHER,
        msg_id="ph4", now=NOW)

    assert result["action"] == "pending"
    assert result["reply"].count("已记下") == 2  # 两条回执都带上了
    assert len(db.list_teacher_intent_pending(GID)) == 2
    assert {uid for uid, _ in result["notify_private"]} == {TEACHER, "root"}
    assert len(model.calls) == 1  # 单窗口只送一次模，数组里两条意图都被处理


async def test_multiple_arrangements_in_long_talk_each_saved_and_merged(env, monkeypatch):
    """长讲话夹多条安排：按窗口逐条落库，回执合并成一条返回。"""
    replies = [
        {"intent": "RECURRING_COURSE", "confidence": "high", "title": "图论",
         "weekday": "周三", "start_time": "19:00", "location": "B302"},
        {"intent": "RECURRING_COURSE", "confidence": "high", "title": "数论",
         "weekday": "周五", "start_time": "19:00", "location": "B303"},
    ]
    counter = {"n": 0}

    async def fake_chat_once(*args, **kwargs):
        payload = replies[min(counter["n"], len(replies) - 1)]
        counter["n"] += 1
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(llm, "_chat_once", fake_chat_once)
    text = (
        "人生如逆水行舟不进则退。每周三晚上7点在B302讲图论，大家要坚持。"
        "有些路要自己走。每周五晚上7点在B303讲数论，别迟到。"
    )
    result = await teacher_intent.handle_message(
        text, group_id=GID, sender_id=TEACHER, msg_id="ph5", now=NOW)

    assert result["action"] == "saved"
    sessions = db.list_training_sessions(GID)
    assert len(sessions) == 2
    assert {(s["title"], s["weekday"]) for s in sessions} == {("图论", "周三"), ("数论", "周五")}
    assert "图论" in result["reply"] and "数论" in result["reply"]  # 两条回执合并
    assert counter["n"] == 2  # 两个候选窗口各送一次模


async def test_pure_philosophy_without_anchor_is_filtered(env, model):
    """纯哲理无任何时间/事件锚点：第一层直接过滤，不花 token、不落库。"""
    result = await teacher_intent.handle_message(
        "人生如逆水行舟不进则退，有些路注定要一个人走，时间会给出所有答案。",
        group_id=GID, sender_id=TEACHER, msg_id="ph6", now=NOW)

    assert result["action"] == "ignored"
    assert model.calls == []
    assert db.list_training_sessions(GID) == []
    assert db.list_teacher_intent_pending(GID) == []
