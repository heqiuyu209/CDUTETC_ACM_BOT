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

import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

import database as db
import fetcher
import llm
from contest_fields import map_competition_info
from config import AUTO_FETCH_ENABLED, FETCH_MIN_INTERVAL_MINUTES, TZ_NAME

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


def run_fetch_sync(now=None, fetch=None, extract=None, send_group=None) -> list[dict]:
    """同步入口：供离线脚本 / 测试在无事件循环环境直接触发一次抓取。

    若当前线程已存在运行中的事件循环（如 bot 协程内被同步调用），
    严禁嵌套 asyncio.run（会抛 RuntimeError），改在独立线程中运行并等待结果；
    命令 /抓取一次 已改为异步调度（await crawler.run_fetch()）。
    """
    import asyncio
    import threading

    def _run() -> list[dict]:
        return asyncio.run(run_fetch(now=now, fetch=fetch, extract=extract,
                                     send_group=send_group))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    # 运行中的事件循环：另起线程 asyncio.run，避免嵌套循环
    box: dict = {}
    def worker():
        try:
            box["result"] = _run()
        except Exception as e:  # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=300)
    if "error" in box:
        raise box["error"]
    if "result" not in box:
        raise TimeoutError("抓取超时")
    return box["result"]

