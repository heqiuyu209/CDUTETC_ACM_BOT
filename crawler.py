"""公共来源集中低频抓取（v2）：默认关闭；开启后变化 → 免审确认/待审，不通知全员。

对应 PRODUCT_SPEC v2 第 4 节：
- 自动抓取默认关闭（AUTO_FETCH_ENABLED，默认 False）；开启后同一来源
  最小间隔由 FETCH_MIN_INTERVAL_MINUTES 控制，集中低频抓取。
- 变化检测三层信号：ETag / Last-Modified / 正文哈希；以正文哈希为强信号，
  只有「实质变化」才调用一次模型提取。
- 提取严格 schema 校验失败不写库（复用 llm.validate_contest_info），
  只记录来源状态为 error，不生成/更新任何记录。
- 免审发布：首次抓取新建比赛直接 confirmed（不生成审核草稿）；
  已确认比赛出现实质变化时正式内容不被覆盖，新内容存待审
  （competition_pending_updates），供超管 /修正 决策。
- v2 默认不通知全员：自动抓取生成的记录只返回结果，不向任何群自动发送；
  超管确认后才通知全员或 /修正 调整/删除。
- 本模块不接触 QQ SDK；发送动作全部由上层注入的回调完成，绝不拿日志冒充发送。
"""

import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import database as db
import fetcher
import llm
from config import AUTO_FETCH_ENABLED, FETCH_MIN_INTERVAL_MINUTES, TZ_NAME
from contest_fields import map_competition_info

logger = logging.getLogger(__name__)

TZ = ZoneInfo(TZ_NAME)

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


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
        - {kind: "auto_created", source_id, competition_id, name,
           action: "created"}    首次抓取免审确认（不通知）
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
        comp = db.get_competition(source["competition_id"])
        name = comp["name"] if comp else f"比赛#{source['competition_id']}"
        if is_within_min_interval(source, now):
            results.append({"kind": "skip", "source_id": source["id"], "reason": "最小抓取间隔未到"})
            continue
        try:
            text, headers = await fetch(source["url"])
        except Exception as e:
            err = str(e) or type(e).__name__
            db.update_source_fetch(
                source["id"], etag="", last_modified="", body_hash="",
                last_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                last_status="error", last_error=err[:500],
            )
            notify = []
            logger.warning("来源抓取失败: %s（%s）", source["url"], err)
            results.append({
                "kind": "error", "source_id": source["id"], "name": name,
                "error": err, "notify_groups": notify,
            })
            continue

        etag = (headers.get("etag") or "").strip()[:500]
        last_modified = (headers.get("last-modified") or "").strip()[:500]
        if not detect_change(source, text, etag, last_modified):
            db.update_source_fetch(
                source["id"], etag=etag, last_modified=last_modified,
                body_hash=source.get("body_hash", ""),
                last_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                last_status="nochange", last_error="",
            )
            results.append({
                "kind": "nochange", "source_id": source["id"],
                "competition_id": source["competition_id"], "name": name,
            })
            continue

        # 实质变化：调一次模型生成提取结果；校验失败不写库
        info, err = await extract(text)
        if err or info is None:
            db.update_source_fetch(
                source["id"], etag=etag, last_modified=last_modified,
                body_hash=source.get("body_hash", ""),
                last_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                last_status="error", last_error=(err or "提取校验失败")[:500],
            )
            notify = []
            results.append({
                "kind": "error", "source_id": source["id"], "name": name,
                "error": err or "提取校验失败", "notify_groups": notify,
            })
            continue

        try:
            comp_info = _map_competition_info(info)
            # 来源地址取自已登记的抓取任务，模型可能漏提或生成错误链接。
            comp_info["official_site"] = source["url"]
            existing = db.get_competition(source["competition_id"])
            if existing is None:
                # v2 免审发布：首次抓取直接确认（不再生成审核草稿）
                cid = db.create_competition(comp_info, status="confirmed",
                                            operator="crawler:auto")
                action = "created"
            elif existing["status"] == "confirmed":
                # 已确认比赛出现实质变化：正式内容不被覆盖，
                # 新内容存待审，供超管 /修正 决策；默认不通知全员。
                update_id = db.add_pending_update(
                    source["competition_id"], comp_info,
                    source_id=source["id"],
                    change_note="公共来源实质变化（免审版不自动替换），待超管确认",
                    created_by="crawler:auto",
                )
                cid = source["competition_id"]
                action = "pending"
            else:
                # 兼容旧草稿状态：直接更新（历史遗留数据）
                db.update_competition(source["competition_id"], comp_info,
                                      operator="crawler:auto",
                                      note="公共来源实质变化，更新草稿")
                cid = source["competition_id"]
                action = "updated"
        except ValueError as e:
            # 提取结果未通过严格 schema 校验（或映射后字段非法）：不写库
            db.update_source_fetch(
                source["id"], etag=etag, last_modified=last_modified,
                body_hash=source.get("body_hash", ""),
                last_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                last_status="error", last_error=(str(e)[:500] or "schema 校验失败"),
            )
            notify = []
            results.append({
                "kind": "error", "source_id": source["id"], "name": name,
                "error": str(e) or "schema 校验失败", "notify_groups": notify,
            })
            continue

        db.update_source_fetch(
            source["id"], etag=etag, last_modified=last_modified,
            body_hash=_body_hash(text),
            last_fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            last_status="confirmed" if action == "created" else (
                "pending" if action == "pending" else "draft"),
            last_error="",
        )
        # v2 不自动通知全员：仅返回结果，由超管确认后统一通知/修正
        item = {
            "kind": {
                "created": "auto_created",
                "pending": "pending_auto",
                "updated": "updated_auto",
            }[action],
            "source_id": source["id"],
            "competition_id": cid, "name": comp_info["name"] or name,
            "action": action, "notify_groups": [],
            "notified": False,
        }
        if action == "pending":
            item["update_id"] = update_id
        results.append(item)
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

