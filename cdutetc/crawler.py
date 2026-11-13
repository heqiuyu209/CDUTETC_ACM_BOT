"""公共来源集中低频抓取（v2）：默认关闭；开启后变化 → 免审确认/待审，不通知全员。

对应 PRODUCT_SPEC v2 第 4 节：
- 自动抓取默认关闭（AUTO_FETCH_ENABLED，默认 False）；开启后同一来源
  最小间隔由 FETCH_MIN_INTERVAL_MINUTES 控制，集中低频抓取。
- 变化检测三层信号：ETag / Last-Modified / 正文哈希；以正文哈希为强信号，
  只有「实质变化」才调用一次模型提取。
- 提取严格 schema 校验失败不写库（复用 llm.validate_contest_info），
  只记录来源状态为 error，不生成/更新任何记录。
- 仅扫描有效关联且未停用的来源；已确认比赛出现实质变化时
  正式内容不被覆盖，新内容存待审
  （competition_pending_updates），供超管 /修正 决策。
- v2 默认不通知全员：自动抓取生成的记录只返回结果，不向任何群自动发送；
  超管确认后才通知全员或 /修正 调整/删除。
- 本模块不接触 QQ SDK；发送动作全部由上层注入的回调完成，绝不拿日志冒充发送。
"""

import bisect
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from html import unescape
from zoneinfo import ZoneInfo

from cdutetc import config
from cdutetc import database as db
from cdutetc import fetcher
from cdutetc import llm
from cdutetc.contest_fields import map_competition_info
from cdutetc.config import AUTO_FETCH_ENABLED, FETCH_MIN_INTERVAL_MINUTES, TZ_NAME

logger = logging.getLogger(__name__)

TZ = ZoneInfo(TZ_NAME)

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
_SOURCE_STATE_FIELDS = (
    "etag", "last_modified", "body_hash", "last_fetched_at", "last_status", "last_error",
)


def _parse_db_time(text: str) -> datetime | None:
    if not text:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_within_min_interval(source: dict, now: datetime) -> bool:
    """是否处于最小抓取间隔内（低频控制）。"""
    last = _parse_db_time(source.get("last_fetched_at") or "")
    if last is None:
        return False
    return (now - last).total_seconds() < FETCH_MIN_INTERVAL_MINUTES * 60


def detect_change(source: dict, text: str, etag: str, last_modified: str) -> bool:
    """判断是否实质变化。

    三层信号：
    1. ETag 变化（弱信号，可能被服务端随意更新）；
    2. Last-Modified 变化（弱信号）；
    3. 正文哈希变化（强信号：页面内容确实不同）。
    以正文哈希为最终判据：哈希不变即认为无实质变化，
    不调用模型、不生成草稿；哈希变化才需要走模型提取。
    """
    if not source.get("body_hash"):
        return True  # 首次抓取，必然算“变化”
    return _body_hash(text) != source["body_hash"]


def _map_competition_info(info: dict) -> dict:
    """保留兼容入口，统一使用公共比赛字段映射。"""
    return map_competition_info(info)


def _source_is_active(source: dict) -> bool:
    return source.get("active", 1) != 0 and source.get("last_status") != "disabled"


def _source_still_active(source: dict) -> bool:
    """等待后核对关联及抓取快照，旧任务不能覆盖后来成功的基线。"""
    comp = db.get_competition(source["competition_id"])
    if comp is None or comp.get("status") not in ("draft", "confirmed"):
        return False
    if hasattr(db, "source_snapshot_matches"):
        return db.source_snapshot_matches(source)
    return any(
        current["id"] == source["id"] and current["url"] == source["url"]
        and _source_is_active(current)
        and all(current.get(field) == source.get(field) for field in _SOURCE_STATE_FIELDS)
        for current in db.list_sources(source["competition_id"])
    )


def _update_source_state(source: dict, **state) -> bool:
    """来源同步接口可用时用条件写入；旧数据库只支持同步快照复查。"""
    if hasattr(db, "source_snapshot_matches"):
        return db.update_source_fetch(source["id"], expected_source=source, **state)
    if not _source_still_active(source):
        return False
    db.update_source_fetch(source["id"], **state)
    return True


def _source_superseded(source: dict) -> dict:
    return {"kind": "skip", "source_id": source["id"], "reason": "来源已停用、关联或抓取基线已更新"}


def _source_error(source: dict, name: str, now: datetime, error: Exception) -> dict:
    err = str(error) or type(error).__name__
    try:
        updated = _update_source_state(
            source, etag=source.get("etag") or "",
            last_modified=source.get("last_modified") or "",
            body_hash=source.get("body_hash") or "",
            last_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            last_status="error", last_error=err[:500],
        )
        if not updated:
            logger.info("忽略过期来源任务的失败: %s（%s）", source["id"], err)
            return _source_superseded(source)
    except Exception as status_error:
        logger.warning("来源错误状态写入失败: %s（%s）", source["id"], status_error)
    logger.warning("来源处理失败: %s（%s）", source["url"], err)
    return {
        "kind": "error", "source_id": source["id"], "name": name,
        "error": err, "notify_groups": [],
    }


async def _process_source(source: dict, comp: dict | None, now: datetime, fetch, extract) -> dict:
    """处理一条来源；异常由扫描层统一隔离并保留成功基线。"""
    sid, cid = source["id"], source["competition_id"]
    if comp is None or comp.get("status") not in ("draft", "confirmed"):
        return {"kind": "skip", "source_id": sid, "reason": "来源关联比赛无效"}
    name = comp["name"]
    if is_within_min_interval(source, now):
        return {"kind": "skip", "source_id": sid, "reason": "最小抓取间隔未到"}

    text, headers = await fetch(source["url"])
    if not isinstance(text, str) or not text.strip():
        raise ValueError("抓取正文必须是非空字符串")
    if not isinstance(headers, Mapping):
        raise ValueError("抓取响应头必须是对象")
    validators = []
    for field in ("etag", "last-modified"):
        value = headers.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"响应头 {field} 必须是字符串")
        validators.append((value or "").strip()[:500])
    etag, last_modified = validators
    if not _source_still_active(source):
        return _source_superseded(source)
    if not detect_change(source, text, etag, last_modified):
        updated = _update_source_state(
            source, etag=etag, last_modified=last_modified,
            body_hash=source.get("body_hash") or "",
            last_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            last_status="nochange", last_error="",
        )
        if not updated:
            return _source_superseded(source)
        return {"kind": "nochange", "source_id": sid, "competition_id": cid, "name": name}

    info, err = await extract(text)
    if err or info is None:
        raise ValueError(str(err) if err else "提取校验失败")
    info, err = llm.validate_contest_info(info)
    if err:
        raise ValueError(err)
    comp_info = _map_competition_info(info)
    # 来源地址取自登记的抓取任务，模型链接不作为写库依据。
    comp_info["official_site"] = source["url"]
    existing = db.get_competition(cid)
    if existing is None or existing.get("status") not in ("draft", "confirmed"):
        return {"kind": "skip", "source_id": sid, "reason": "来源关联比赛无效"}
    if not _source_still_active(source):
        return _source_superseded(source)
    new_source_state = {
        "etag": etag, "last_modified": last_modified, "body_hash": _body_hash(text),
        "last_fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if hasattr(db, "store_crawled_competition"):
        # 同事务核对快照并保存内容/基线，覆盖最终复查到写入的并发窗口。
        stored = db.store_crawled_competition(source, comp_info, new_source_state)
        if stored is None:
            return _source_superseded(source)
        action = stored["action"]
        if action == "pending":
            update_id = stored["update_id"]
    elif existing["status"] == "confirmed":
        update_id = db.add_pending_update(
            cid, comp_info, source_id=sid,
            change_note="公共来源实质变化（免审版不自动替换），待超管确认",
            created_by="crawler:auto",
        )
        action = "pending"
    else:
        if not db.update_competition(cid, comp_info, operator="crawler:auto",
                                     note="公共来源实质变化，更新草稿"):
            raise ValueError("来源关联比赛更新失败")
        action = "updated"
    if not hasattr(db, "store_crawled_competition"):
        if not _update_source_state(
            source, **new_source_state,
            last_status="pending" if action == "pending" else "draft", last_error="",
        ):
            return _source_superseded(source)
    item = {
        "kind": "pending_auto" if action == "pending" else "updated_auto",
        "source_id": sid, "competition_id": cid, "name": comp_info["name"],
        "action": action, "notify_groups": [], "notified": False,
    }
    if action == "pending":
        item["update_id"] = update_id
    return item


async def run_fetch(now: datetime | None = None,
                    fetch=None,
                    extract=None,
                    send_group=None) -> list[dict]:
    """集中抓取全部公共来源，返回结果清单（不直接发送任何消息）。

    Args:
        now: 当前时间（北京时区）；默认取系统当前时间。
        fetch: 可注入的抓取回调 async (url) -> (text, headers)，默认 fetcher.fetch_text_with_headers。
        extract: 可注入的提取回调 async (text) -> (info, err)，默认 llm.extract_contest。
        send_group: 兼容参数（v2 不再自动发送全员通知，保留以对接旧调用方）。

    Returns:
        每条结果 dict：
        - {kind: "disabled"}                          AUTO_FETCH_ENABLED=False 整体跳过
        - {kind: "skip", source_id, reason}            未到抓取间隔/无来源
        - {kind: "nochange", source_id, competition_id, name}   无实质变化
        - {kind: "pending_auto", source_id, competition_id, name,
           action: "pending", update_id}   已确认比赛变化存待审（不通知）
        - {kind: "updated_auto", source_id, competition_id, name,
           action: "updated"}    草稿状态比赛更新（不通知）
        - {kind: "error", source_id, name, error, notify_groups: [...]}  抓取或提取失败
    """
    if now is None:
        now = datetime.now(TZ)
    fetch = fetch or fetcher.fetch_text_with_headers
    extract = extract or llm.extract_contest
    results: list[dict] = []
    if not AUTO_FETCH_ENABLED:
        results.append({"kind": "disabled", "reason": "自动抓取未开启"})
        return results

    for source in db.list_sources():
        name = f"比赛#{source['competition_id']}"
        try:
            if not _source_is_active(source):
                results.append({"kind": "skip", "source_id": source["id"], "reason": "来源已停用"})
                continue
            comp = db.get_competition(source["competition_id"])
            if comp is not None:
                name = comp.get("name") or name
            results.append(await _process_source(source, comp, now, fetch, extract))
        except Exception as error:
            # 包含抓取、提取、校验、比赛写入和状态写入；只失败当前来源。
            results.append(_source_error(source, name, now, error))
    return results


# ================= 需求 F：Codeforces / 牛客 赛程自动抓取 =================
# 与上面的「按来源抓正文 + 模型提取」链路完全独立：这里只解析结构化的赛程
# 清单（CF 官方 API、牛客赛程页），默认关闭（AUTO_CONTEST_CRAWL_ENABLED），
# 抓到的比赛按 AUTO_CONTEST_AUTO_CONFIRM 决定直接发布还是进草稿待审。

CF_CONTEST_API = "https://codeforces.com/api/contest.list"
NOWCODER_CONTEST_PAGE = "https://ac.nowcoder.com/acm/contest/vip-index"
# 牛客官方日历 JSON 接口：按月返回赛程，服务器直连通畅；
# 网页 vip-index 是前端渲染页，服务端抓到的常常是空壳，故改为接口优先、页面兜底。
NOWCODER_CALENDAR_API = "https://ac.nowcoder.com/acm/calendar/contest?token=&month={month}"
NOWCODER_REFERER = NOWCODER_CONTEST_PAGE
_CF_URL_TEMPLATE = "https://codeforces.com/contest/{id}"
_NOWCODER_URL_TEMPLATE = "https://ac.nowcoder.com/acm/contest/{id}"
# 日历接口里 contestId（如 1143041）与真实路径 ID（如 143041）不一致，
# 因此 URL 必须从 link 字段取；模板只用于 link 缺失时的兼容回退。
_NOWCODER_LINK_ID = re.compile(r"/acm/contest/(\d+)")
_NOWCODER_ANCHOR = re.compile(
    r'<a[^>]*href="[^"]*/acm/contest/(\d+)"[^>]*>(.*?)</a>', re.S | re.I)
_NOWCODER_ID = re.compile(r"/acm/contest/(\d+)")
_NOWCODER_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")
# 页面上的时段写法：「19:00-21:00」，分隔符可能是 - ~ ～ — 到 至。
_NOWCODER_RANGE = re.compile(r"(\d{1,2}:\d{2})\s*[-~～—至到]\s*(\d{1,2}:\d{2})")
_TAG = re.compile(r"<[^>]+>")
# 列表项的块边界：用于把「比赛标题」与「时间」限定在同一列表项内。
_NOWCODER_BLOCK_END = re.compile(r"</(?:div|li|tr|p|dl|dd|section|article)>", re.I)


def _as_tz(now: datetime | None) -> datetime:
    """把任意传入时间统一成北京时间 aware datetime。"""
    if now is None:
        return datetime.now(TZ)
    return now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)


def _fmt_minute(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _clean_name(text: str) -> str:
    """清掉锚文本里的标签、HTML 实体与多余空白。"""
    plain = _TAG.sub(" ", str(text or ""))
    plain = unescape(plain).replace("\xa0", " ")
    return " ".join(plain.split())


def _window_guard(start: datetime, now: datetime, window_days: int) -> bool:
    """仅保留「尚未开始且落在窗口内」的比赛。"""
    delta = (start - now).total_seconds()
    return 0 <= delta <= max(int(window_days), 0) * 86400


def _nowcoder_window_guard(start: datetime, end: datetime | None,
                           now: datetime, window_days: int) -> bool:
    """牛客赛程窗口：未结束且在窗口内的比赛都要，当天已开赛的不能漏。

    - 有结束时间：只要 ``end > now`` 就算未结束（哪怕已经开始）；
    - 无结束时间：允许开始时间回看 3 小时，覆盖「刚开赛但接口没给 end」的情况；
    - 上限仍受 ``window_days`` 约束，避免把远期赛程全部拉进。
    """
    limit = now + timedelta(days=max(int(window_days), 0))
    if start > limit:
        return False
    if end is not None:
        return end > now
    return start >= now - timedelta(hours=3)


def _nowcoder_calendar_urls(now: datetime, window_days: int) -> list[str]:
    """按月构造日历接口地址：窗口跨月时同时取本月与后续月份。"""
    first = now.replace(day=1)
    last = now + timedelta(days=max(int(window_days), 0))
    months: list[str] = []
    cursor = first
    while cursor <= last and len(months) < 3:
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return [NOWCODER_CALENDAR_API.format(month=month) for month in months]


def _nowcoder_clean_link(value) -> str | None:
    """从 link / url 字段取干净的比赛地址，剥离 ``?from=acm_calendar`` 等追踪参数。"""
    text = str(value or "").strip()
    if not text:
        return None
    match = _NOWCODER_LINK_ID.search(text)
    if match is None:
        return None
    return _NOWCODER_URL_TEMPLATE.format(id=int(match.group(1)))



def parse_codeforces(payload: dict, now: datetime, window_days: int = 14) -> list[dict]:
    """解析 Codeforces ``contest.list`` 响应，返回窗口内的未开始比赛。

    只认 ``phase == "BEFORE"``；``status != "OK"``、字段缺失或类型异常的条目
    一律跳过，任何输入都不抛异常（抓取链路不能让脏数据打崩定时任务）。
    """
    if not isinstance(payload, Mapping) or payload.get("status") != "OK":
        return []
    raw_items = payload.get("result")
    if not isinstance(raw_items, list):
        return []
    current = _as_tz(now)
    parsed: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        try:
            name = str(raw.get("name") or "").strip()
            contest_id = int(raw["id"])
            start_seconds = int(raw["startTimeSeconds"])
            duration_minutes = int(raw["durationSeconds"]) // 60
            start = datetime.fromtimestamp(start_seconds, TZ)
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
        if not name or raw.get("phase") != "BEFORE":
            continue
        if not _window_guard(start, current, window_days):
            continue
        end = datetime.fromtimestamp(start_seconds + duration_minutes * 60, TZ)
        parsed.append({
            "platform": "Codeforces",
            "name": name,
            "start": _fmt_minute(start),
            "end": _fmt_minute(end),
            "duration_minutes": duration_minutes,
            "url": _CF_URL_TEMPLATE.format(id=contest_id),
        })
    parsed.sort(key=lambda item: item["start"])
    return parsed


def _nowcoder_time(value) -> datetime | None:
    """牛客时间字段：支持时间戳（秒/毫秒）与常见字符串格式。"""
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, TZ)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _nowcoder_items(payload) -> list:
    """从牛客 JSON 的常见包装里取出比赛数组。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    for key in ("contests", "list", "data", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, Mapping):
            nested = _nowcoder_items(value)
            if nested:
                return nested
    return []


def _nowcoder_from_json(text: str, now: datetime, window_days: int) -> list[dict]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return []
    return _nowcoder_items_to_contests(_nowcoder_items(payload), now, window_days)


def _nowcoder_items_to_contests(items, now: datetime, window_days: int) -> list[dict]:
    """把日历接口的比赛数组转成统一赛程条目（URL 优先取 link，窗口放行当日赛事）。"""
    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        link_url = _nowcoder_clean_link(
            raw.get("link") or raw.get("url") or raw.get("contestUrl"))
        contest_id = raw.get("contestId") or raw.get("id")
        name = _clean_name(raw.get("name") or raw.get("title")
                           or raw.get("contestName") or raw.get("contestTitle")
                           or raw.get("contest_name") or "")
        start = _nowcoder_time(
            raw.get("startTime") or raw.get("start_time") or raw.get("start"))
        if not name or start is None:
            continue
        if link_url is None and not contest_id:
            # 两者都没有时无法定位比赛，宁可不收也不编造链接。
            continue
        duration = raw.get("duration") or raw.get("durationSeconds") or 0
        try:
            minutes = int(duration) // 60 if int(duration) > 600 else int(duration)
        except (TypeError, ValueError):
            minutes = 0
        end_value = _nowcoder_time(raw.get("endTime") or raw.get("end_time"))
        if end_value is not None and end_value > start:
            # 只给了结束时刻时用差值补时长，避免 end 与 duration_minutes 自相矛盾。
            minutes = int((end_value - start).total_seconds() // 60)
        if not _nowcoder_window_guard(start, end_value, now, window_days):
            continue
        end = end_value or (start + timedelta(minutes=max(minutes, 0)))
        url = link_url
        if url is None:
            try:
                url = _NOWCODER_URL_TEMPLATE.format(id=int(contest_id))
            except (TypeError, ValueError):
                continue
        out.append({
            "platform": "牛客",
            "name": name,
            "start": _fmt_minute(start),
            "end": _fmt_minute(end),
            "duration_minutes": max(minutes, 0),
            "url": url,
        })
    out.sort(key=lambda item: item["start"])
    return out



def _nowcoder_blocks(text: str) -> list[tuple[int, int]]:
    """按块结束标签把 HTML 切成列表项区间 ``[(start, end), ...]``。"""
    blocks: list[tuple[int, int]] = []
    start = 0
    for boundary in _NOWCODER_BLOCK_END.finditer(text):
        blocks.append((start, boundary.start()))
        start = boundary.end()
    blocks.append((start, len(text)))
    return blocks


def _nowcoder_from_html(text: str, now: datetime, window_days: int) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    blocks = _nowcoder_blocks(text)
    block_starts = [start for start, _ in blocks]
    block_has_anchor = [
        _NOWCODER_ANCHOR.search(text[start:end]) is not None for start, end in blocks
    ]
    for match in _NOWCODER_ANCHOR.finditer(text):
        contest_id = int(match.group(1))
        name = _clean_name(match.group(2))
        if not name or contest_id in seen:
            continue
        # 时间与标题同属一个列表项：只在锚所在块内、锚之后取时间；
        # 块内没有才回看上一块（且上一块不能含别的比赛锚），避免张冠李戴。
        index = max(bisect.bisect_right(block_starts, match.start()) - 1, 0)
        seg_start, seg_end = blocks[index]
        block_text = text[seg_start:seg_end]
        if len(_NOWCODER_ID.findall(block_text)) <= 1:
            # 一个列表项只挂一场比赛：时间写在标题前还是后都归它。
            segment = block_text
        else:
            segment = text[match.end():seg_end]
        stamp = (_NOWCODER_DATETIME.search(text[match.end():seg_end])
                 or _NOWCODER_DATETIME.search(segment))
        if stamp is None and index > 0 and not block_has_anchor[index - 1]:
            prev_start, prev_end = blocks[index - 1]
            segment = text[prev_start:prev_end]
            found = list(_NOWCODER_DATETIME.finditer(segment))
            stamp = found[-1] if found else None
        if stamp is None:
            continue
        start = _nowcoder_time(f"{stamp.group(1)} {stamp.group(2)}")
        if start is None or not _nowcoder_window_guard(start, None, now, window_days):
            continue
        # 锚后片段若给出同日时段（如「19:00-21:00」），用它补出结束时间与时长；
        # 结束时刻不晚于开始时刻视为跨天（如 23:00-01:00），仍属同一条比赛。
        end, minutes = start, 0
        span = _NOWCODER_RANGE.search(segment)
        if span is not None:
            end_time = _nowcoder_time(f"{stamp.group(1)} {span.group(2)}")
            if end_time is not None:
                if end_time <= start:
                    end_time += timedelta(days=1)
                end = end_time
                minutes = int((end_time - start).total_seconds() // 60)
        seen.add(contest_id)
        out.append({
            "platform": "牛客",
            "name": name,
            "start": _fmt_minute(start),
            "end": _fmt_minute(end),
            "duration_minutes": minutes,
            "url": _NOWCODER_URL_TEMPLATE.format(id=contest_id),
        })
    out.sort(key=lambda item: item["start"])
    return out


def parse_nowcoder(text: str, now: datetime, window_days: int = 14) -> list[dict]:
    """解析牛客赛程：JSON 优先，失败再按 HTML 正则解析。"""
    if not isinstance(text, str) or not text.strip():
        return []
    current = _as_tz(now)
    parsed = _nowcoder_from_json(text, current, window_days)
    if parsed:
        return parsed
    return _nowcoder_from_html(text, current, window_days)


def dedupe_contest_items(items) -> list[dict]:
    """按 (platform, name, start) 去重，保留首个。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        key = (str(item.get("platform") or ""), str(item.get("name") or ""),
               str(item.get("start") or ""))
        if not key[1] or not key[2] or key in seen:
            continue
        seen.add(key)
        out.append(dict(item))
    return out


async def _fetch_nowcoder_contests(current: datetime, window: int, nc_fetcher) -> list[dict]:
    """牛客赛程：日历 JSON 接口优先（跨月合并去重），异常再降级赛程页 HTML。"""
    items: list[dict] = []
    json_ok = False
    for url in _nowcoder_calendar_urls(current, window):
        try:
            text = await nc_fetcher(url)
        except Exception as error:  # noqa: BLE001 - 单月失败要能继续试其它月份
            logger.warning("牛客日历接口请求失败（%s）：%s", url, error)
            continue
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            logger.warning("牛客日历接口返回非 JSON（%s），准备降级赛程页", url)
            continue
        json_ok = True
        items.extend(_nowcoder_items_to_contests(
            _nowcoder_items(payload), current, window))
    if json_ok:
        # 跨月请求常出现同一场比赛（月末/月初边界），按 (平台, 名称, 开始时间) 去重。
        return dedupe_contest_items(items)
    logger.warning("牛客日历接口不可用，降级抓取赛程页：%s", NOWCODER_CONTEST_PAGE)
    page = await nc_fetcher(NOWCODER_CONTEST_PAGE)
    if not isinstance(page, str):
        page = json.dumps(page, ensure_ascii=False)
    return dedupe_contest_items(parse_nowcoder(page, current, window))


async def fetch_contests(now: datetime | None = None, *,
                         codeforces_fetch=None, nowcoder_fetch=None) -> dict:
    """抓取两站赛程；单源失败只记错误项，不影响另一源。"""
    current = _as_tz(now)
    window = config.AUTO_CONTEST_WINDOW_DAYS
    cf_fetcher = codeforces_fetch or _http_get_text
    nc_fetcher = nowcoder_fetch or _nowcoder_http_get_text
    result = {"codeforces": [], "nowcoder": [], "errors": []}

    try:
        payload = await cf_fetcher(CF_CONTEST_API)
        if isinstance(payload, str):
            payload = json.loads(payload)
        result["codeforces"] = parse_codeforces(payload, current, window)
    except Exception as error:  # noqa: BLE001 - 单源失败必须隔离
        logger.warning("Codeforces 赛程抓取失败：%s", error)
        result["errors"].append({"platform": "Codeforces", "error": str(error)})

    try:
        result["nowcoder"] = await _fetch_nowcoder_contests(current, window, nc_fetcher)
    except Exception as error:  # noqa: BLE001 - 单源失败必须隔离
        logger.warning("牛客赛程抓取失败：%s", error)
        result["errors"].append({"platform": "牛客", "error": str(error)})
    return result


async def _http_get_text(url: str) -> str:
    """复用既有抓取通道（同一套 SSRF 防护、超时与 UA）。"""
    return await fetcher.fetch_text(url)


async def _nowcoder_http_get_text(url: str) -> str:
    """牛客请求带上来源页 Referer：日历接口对裸请求偶发返回空/阻断。"""
    return await fetcher.fetch_text_with_extra_headers(
        url, {"Referer": NOWCODER_REFERER})


def _existing_contest_keys() -> set[tuple[str, str]]:
    """库内既有比赛的 (名称, 官方时间) 指纹。"""
    keys: set[tuple[str, str]] = set()
    for comp in db.list_competitions():
        keys.add((str(comp.get("name") or "").strip(),
                  str(comp.get("official_time") or "").strip()))
    return keys


def _contest_notify_groups() -> list[str]:
    configured = sorted(config.AUTO_CONTEST_NOTIFY_GROUPS)
    return configured or sorted(config.ALLOWED_GROUPS)


async def run_contest_fetch(now: datetime | None = None, *, fetchers=None) -> list[dict]:
    """抓取 → 去重 → 与库内比对 → 建档，返回逐条处理结果。"""
    current = _as_tz(now)
    fetchers = fetchers or {}
    out: list[dict] = []
    if not config.AUTO_CONTEST_CRAWL_ENABLED:
        return [{"kind": "disabled", "reason": "自动赛程抓取未开启"}]

    try:
        fetched = await fetch_contests(current, **fetchers)
    except Exception as error:  # noqa: BLE001 - 定时任务不得因抓取崩掉
        return [{"kind": "error", "error": str(error)}]

    for problem in fetched.get("errors", []):
        out.append({"kind": "error", "platform": problem.get("platform"),
                    "error": problem.get("error")})
    items = dedupe_contest_items(list(fetched.get("codeforces", []))
                                 + list(fetched.get("nowcoder", [])))
    existing = _existing_contest_keys()
    for item in items:
        name, start = item.get("name"), item.get("start")
        entry = {"platform": item.get("platform"), "name": name,
                 "start": start, "url": item.get("url")}
        if (str(name).strip(), str(start).strip()) in existing:
            out.append({**entry, "kind": "exists"})
            continue
        info = {
            "name": str(name),
            "edition": item.get("platform") or "",
            "official_time": start,
            "official_site": item.get("url") or "",
            "summary": f"{item.get('platform')} 自动抓取",
        }
        status = "confirmed" if config.AUTO_CONTEST_AUTO_CONFIRM else "draft"
        try:
            cid = db.create_competition(info, status=status, operator="auto-fetch")
        except Exception as error:  # noqa: BLE001 - 单条失败不影响整批
            out.append({**entry, "kind": "error", "error": str(error)})
            continue
        if status == "confirmed":
            groups = _contest_notify_groups()
            if groups:
                db.set_competition_notify_groups(cid, groups)
        existing.add((str(name).strip(), str(start).strip()))
        out.append({**entry, "kind": "created", "competition_id": cid})
    return out
