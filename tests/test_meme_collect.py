"""零审核自动收图与场景化发图离线测试。

覆盖：按群开关、截图/小图/大图过滤、上下文归类、MD5 去重与黑名单、
/删图 拉黑、连续问号触发、最近 10 张防重、纯 [MEME:xx] 接话。
"""

import io
import sqlite3
from datetime import datetime

import pytest

from cdutetc import config, database, memes, persona_chat

TZ = database.TZ


# ---------------- 公共夹具 ----------------

@pytest.fixture
def db_conn(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


def make_image(size=(400, 400), fmt="PNG", color=(90, 160, 90)) -> bytes:
    """用 Pillow 造一张真实可解析的图，尺寸/格式可指定。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


class FakeMessage:
    """最小群消息替身：只提供 extract_image_refs 需要的两个属性。"""

    def __init__(self, urls, content="") -> None:
        self.raw = {"message": [
            {"type": "image", "data": {"url": url, "file": url.rsplit("/", 1)[-1]}}
            for url in urls
        ]}
        self.content = content


def make_downloader(payload: bytes, calls: list[str] | None = None):
    async def download(url: str) -> bytes:
        if calls is not None:
            calls.append(url)
        return payload

    return download


# ---------------- 开关 ----------------

@pytest.mark.asyncio
async def test_collect_skipped_when_switch_off(tmp_path, db_conn):
    """开关关闭：一张都不收，也不去下载（省流量、省磁盘）。"""
    calls: list[str] = []
    message = FakeMessage(["http://example.com/a.png"])
    collected = await memes.collect_from_message(
        message, group_id="g1", text="这题 WA 了", root=tmp_path,
        downloader=make_downloader(make_image(), calls),
        enabled_check=lambda group: False,
    )
    assert collected == []
    assert calls == []
    assert not (tmp_path / memes.CATEGORY_WA_TLE).exists()


@pytest.mark.asyncio
async def test_collect_enabled_by_group_setting(tmp_path, db_conn):
    """开关状态直接读 group_configs：默认关，/偷图 开后立刻生效。"""
    message = FakeMessage(["http://example.com/a.png"])
    downloader = make_downloader(make_image())

    assert database.meme_collect_enabled("g1") is False
    assert await memes.collect_from_message(
        message, group_id="g1", text="绝杀", root=tmp_path, downloader=downloader,
    ) == []

    database.set_meme_collect_enabled("g1", True)
    collected = await memes.collect_from_message(
        message, group_id="g1", text="绝杀", root=tmp_path, downloader=downloader,
    )
    assert len(collected) == 1
    assert collected[0]["category"] == memes.CATEGORY_AC


# ---------------- 过滤 ----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("size,suffix,expect", [
    ((1200, 400), ".png", False),   # 宽高比 3.0：代码长截图
    ((400, 1200), ".png", False),   # 宽高比 0.33：手机长截图
    ((60, 60), ".png", False),      # 小图标
    ((400, 400), ".png", True),     # 正常方图
    ((400, 1200), ".gif", True),    # 动图直接放行，不看比例
])
async def test_collect_filters_screenshots_and_icons(tmp_path, db_conn, size,
                                                     suffix, expect):
    data = make_image(size=size, fmt="GIF" if suffix == ".gif" else "PNG")
    message = FakeMessage([f"http://example.com/a{suffix}"])
    collected = await memes.collect_from_message(
        message, group_id="g1", text="离谱", root=tmp_path,
        downloader=make_downloader(data), enabled_check=lambda group: True,
    )
    assert bool(collected) is expect


def test_should_collect_rejects_oversized_photo():
    """体积 > 2MB 的高清照片不收：直接看字节数，不用真造大图。"""
    accepted, reason = memes.should_collect(
        b"x" * (memes.COLLECT_MAX_BYTES + 1), suffix=".png")
    assert accepted is False
    assert "MB" in reason or "KB" in reason


# ---------------- 上下文归类 ----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("text,context,category", [
    ("", ("这题又 WA 了",), memes.CATEGORY_WA_TLE),        # 靠上一条文本判
    ("TLE 到怀疑人生", (), memes.CATEGORY_WA_TLE),
    ("绝杀！", (), memes.CATEGORY_AC),
    ("？？", (), memes.CATEGORY_QUESTION),
    ("今天好热", (), memes.CATEGORY_SLACKING),            # 认不出就归摸鱼
])
async def test_collect_classifies_by_context(tmp_path, db_conn, text, context,
                                            category):
    message = FakeMessage(["http://example.com/a.png"])
    collected = await memes.collect_from_message(
        message, group_id="g1", text=text, context=context, root=tmp_path,
        downloader=make_downloader(make_image()), enabled_check=lambda group: True,
    )
    assert len(collected) == 1
    assert collected[0]["category"] == category
    assert (tmp_path / category).is_dir()


@pytest.mark.asyncio
async def test_collected_filename_has_tag_and_md5_prefix(tmp_path, db_conn):
    """命名规则 {tag}_{md5[:8]}.{ext}：标签是日后按文本优先抽图的依据。"""
    data = make_image()
    collected = await memes.collect_from_message(
        FakeMessage(["http://example.com/a.png"]), group_id="g1", text="AC 了",
        root=tmp_path, downloader=make_downloader(data),
        enabled_check=lambda group: True,
    )
    relative = collected[0]["relative_path"]
    name = relative.rsplit("/", 1)[-1]
    md5 = memes.image_md5(data)
    assert relative.startswith(f"{memes.CATEGORY_AC}/")
    assert name.endswith(f"_{md5[:8]}.png")
    assert memes.filename_tag(relative)


# ---------------- 去重与黑名单 ----------------

@pytest.mark.asyncio
async def test_collect_dedupes_same_image(tmp_path, db_conn):
    data = make_image()
    downloader = make_downloader(data)
    message = FakeMessage(["http://example.com/a.png"])
    first = await memes.collect_from_message(
        message, group_id="g1", text="绝杀", root=tmp_path,
        downloader=downloader, enabled_check=lambda group: True)
    second = await memes.collect_from_message(
        message, group_id="g1", text="绝杀", root=tmp_path,
        downloader=downloader, enabled_check=lambda group: True)
    assert len(first) == 1
    assert second == []
    assert database.count_meme_hashes() == 1


@pytest.mark.asyncio
async def test_collect_skips_blacklisted_image(tmp_path, db_conn):
    """deleted=1 的图是黑名单：删过的不再被同一个群里第二次收进来。"""
    data = make_image()
    md5 = memes.image_md5(data)
    database.save_meme_hash(md5, memes.CATEGORY_AC, "ac", "ac/old.png")
    database.mark_meme_hash_deleted(md5)
    collected = await memes.collect_from_message(
        FakeMessage(["http://example.com/a.png"]), group_id="g1", text="绝杀",
        root=tmp_path, downloader=make_downloader(data),
        enabled_check=lambda group: True,
    )
    assert collected == []


# ---------------- /删图 ----------------

@pytest.mark.asyncio
async def test_delete_last_sent_removes_file_and_blacklists(tmp_path, db_conn):
    data = make_image()
    collected = await memes.collect_from_message(
        FakeMessage(["http://example.com/a.png"]), group_id="g1", text="绝杀",
        root=tmp_path, downloader=make_downloader(data),
        enabled_check=lambda group: True,
    )
    relative = collected[0]["relative_path"]
    engine = memes.MemeEngine(root=tmp_path)
    engine.record_sent("g1", relative)

    outcome = memes.delete_last_sent("g1", engine=engine, root=tmp_path)
    assert outcome is not None
    assert outcome["relative_path"] == relative
    assert outcome["md5"] == collected[0]["md5"]
    assert not (tmp_path / relative).exists()
    assert engine.recent_sent("g1") == []
    assert database.get_meme_hash(outcome["md5"])["deleted"] == 1


def test_delete_last_sent_without_history_returns_none(tmp_path):
    engine = memes.MemeEngine(root=tmp_path)
    assert memes.delete_last_sent("g-empty", engine=engine, root=tmp_path) is None


# ---------------- 场景化触发 ----------------

def _engine(tmp_path, clock=None):
    for category in memes.CATEGORIES:
        directory = tmp_path / category
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "1.png").write_bytes(b"fake-png")
    return memes.MemeEngine(root=tmp_path, clock=clock or (lambda: 0.0),
                            rng=lambda: 0.0)


def test_scene_trigger_on_consecutive_questions(tmp_path):
    """连续两条带问号的短句 → 直接发 question 图，不需要命中任何关键词。"""
    engine = _engine(tmp_path)
    engine.remember_text("g1", "这题怎么做？")
    assert engine.scene_trigger("g1", "为啥啊?") == memes.CATEGORY_QUESTION


def test_scene_trigger_ignores_single_question(tmp_path):
    engine = _engine(tmp_path)
    engine.remember_text("g1", "今天天气不错")
    assert engine.scene_trigger("g1", "为啥啊?") == ""


def test_scene_trigger_slacking_at_meal_time(tmp_path):
    engine = _engine(tmp_path)
    moment = datetime(2026, 11, 15, 12, 10, tzinfo=TZ)
    assert engine.scene_trigger("g1", "饿了，吃啥？", moment=moment) == (
        memes.CATEGORY_SLACKING)


def test_scene_trigger_slacking_late_night(tmp_path):
    engine = _engine(tmp_path)
    moment = datetime(2026, 11, 15, 1, 20, tzinfo=TZ)
    assert engine.scene_trigger("g1", "困死了想睡觉", moment=moment) == (
        memes.CATEGORY_SLACKING)


def test_scene_trigger_respects_8_minute_cooldown(tmp_path):
    clock = {"now": 100.0}
    engine = _engine(tmp_path, clock=lambda: clock["now"])
    moment = datetime(2026, 11, 15, 12, 10, tzinfo=TZ)
    assert engine.scene_trigger("g1", "饿了吃啥", moment=moment) == (
        memes.CATEGORY_SLACKING)
    clock["now"] += memes.SCENE_COOLDOWN_SECONDS - 1
    assert engine.scene_trigger("g1", "饿了吃啥", moment=moment) == ""
    clock["now"] += 2
    assert engine.scene_trigger("g1", "饿了吃啥", moment=moment) == (
        memes.CATEGORY_SLACKING)


# ---------------- 最近 10 张防重 ----------------

def test_recent_sent_queue_limits_to_10(tmp_path):
    engine = _engine(tmp_path)
    for index in range(12):
        engine.record_sent("g1", f"ac/{index}.png")
    recent = engine.recent_sent("g1")
    assert len(recent) == memes.RECENT_SENT_LIMIT
    assert recent[-1] == "ac/11.png"
    assert "ac/0.png" not in recent


def test_resolve_avoids_recently_sent(tmp_path, monkeypatch):
    """用过的那张会被跳过，同一分类里还有别的图就换一张。"""
    monkeypatch.setattr(config, "MEME_BASE_URL", "https://cdn.example.com/memes")
    engine = _engine(tmp_path)
    (tmp_path / memes.CATEGORY_AC / "2.png").write_bytes(b"fake-png")
    engine.record_sent("g1", "ac/1.png")
    trigger = engine.resolve(memes.CATEGORY_AC, group_id="g1")
    assert trigger is not None
    assert trigger.relative_path == "ac/2.png"


def test_resolve_prefers_tag_matched_file(tmp_path, monkeypatch):
    """按聊天文本命中的标签前缀优先选图，比纯随机更贴题。"""
    monkeypatch.setattr(config, "MEME_BASE_URL", "https://cdn.example.com/memes")
    engine = _engine(tmp_path)
    (tmp_path / memes.CATEGORY_AC / "绝杀_12345678.png").write_bytes(b"png")
    trigger = engine.resolve(memes.CATEGORY_AC, group_id="g1", text="这题真的绝杀")
    assert trigger is not None
    assert trigger.relative_path.endswith("绝杀_12345678.png")


def test_resolve_without_base_url_can_skip_url(tmp_path, monkeypatch):
    """OneBot 直接发本机文件：没有公网地址也能解析出相对路径。"""
    monkeypatch.setattr(config, "MEME_BASE_URL", "")
    engine = _engine(tmp_path)
    assert engine.resolve(memes.CATEGORY_AC) is None
    trigger = engine.resolve(memes.CATEGORY_AC, require_url=False)
    assert trigger is not None
    assert trigger.url == ""
    assert memes.meme_path(trigger.relative_path, root=tmp_path).is_file()


def test_meme_path_rejects_escape(tmp_path):
    assert memes.meme_path("../secret.png", root=tmp_path) is None
    assert memes.meme_path("ac/missing.png", root=tmp_path) is None


# ---------------- 纯 [MEME:xx] 接话 ----------------

def test_split_meme_tag_allows_meme_only_reply():
    """模型只给标签、不写字时：文本为空，但分类被识别出来（只发图不发空文本）。"""
    text, category = persona_chat.split_meme_tag("[MEME:slacking]")
    assert text == ""
    assert category == memes.CATEGORY_SLACKING


def test_split_meme_tag_strips_tag_from_text():
    text, category = persona_chat.split_meme_tag("补题去吧 [MEME:wa_tle]")
    assert text == "补题去吧"
    assert category == memes.CATEGORY_WA_TLE


def test_split_meme_tag_rejects_unknown_category():
    """未知目录的标签同样被剥离（内标记不能泄漏），但分类返回空表示不发图。"""
    text, category = persona_chat.split_meme_tag("[MEME:notadir]")
    assert text == ""
    assert category == ""
