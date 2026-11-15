"""自动收图链路升级（OCR / 截图拦截 / dHash 去重 / 单人限流 / 分类 FIFO）测试。

全部离线：OCR 用 monkeypatch 注入假文本，图片用 Pillow 现场生成，不联网不调模型。
"""

import io
import random
import sqlite3

import pytest

from cdutetc import config, database, memes


# ---------------- 公共夹具 ----------------

@pytest.fixture
def db_conn(monkeypatch):
    """内存库 + 干净限流台账，保证用例之间互不污染。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    memes.reset_collect_limits()
    yield conn
    conn.close()
    memes.reset_collect_limits()


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch):
    """默认「没装 OCR」：单测零外部依赖，需要文字的场景自行覆盖。"""
    monkeypatch.setattr(memes, "extract_image_text", lambda data: "")


def make_image(seed: int = 0, size: tuple[int, int] = (240, 240)) -> bytes:
    """造一张随机噪声图：既真实可解析，又能保证各图 dHash 明显不同。"""
    from PIL import Image

    rnd = random.Random(seed)
    image = Image.new("L", size)
    image.putdata([rnd.randrange(256) for _ in range(size[0] * size[1])])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_flat_image(shade: int = 120, size: tuple[int, int] = (240, 240)) -> bytes:
    """纯色图：所有像素相同，dHash 全 0，专供 dHash 边界用例。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", size, shade).save(buffer, format="PNG")
    return buffer.getvalue()


def downloader_of(payload: bytes):
    async def download(url: str) -> bytes:
        return payload

    return download


async def collect(payload: bytes, *, root, text: str = "", user_id: str = "u1",
                  group_id: str = "g1"):
    return await memes.collect_one_image(
        {"url": "http://example.com/a.png", "file": "a.png"},
        group_id=group_id, text=text, root=root,
        downloader=downloader_of(payload), user_id=user_id,
    )


# ---------------- OCR 参与归类 ----------------

@pytest.mark.asyncio
async def test_ocr_text_drives_category(tmp_path, db_conn, monkeypatch):
    """图内文字优先于群聊上下文：纯图也能按图里的「WA」归到 wa_tle。"""
    monkeypatch.setattr(memes, "extract_image_text", lambda data: "又 WA 了")
    outcome = await collect(make_image(1), root=tmp_path, text="今天吃啥")
    assert outcome is not None
    assert outcome["category"] == memes.CATEGORY_WA_TLE


@pytest.mark.asyncio
async def test_ocr_text_beats_context_keywords(tmp_path, db_conn, monkeypatch):
    """图里写「AC」而群聊说「WA」时，以图内文字为准。"""
    monkeypatch.setattr(memes, "extract_image_text", lambda data: "AC 拿下")
    outcome = await collect(make_image(2), root=tmp_path, text="这发 WA 寄了")
    assert outcome is not None
    assert outcome["category"] == memes.CATEGORY_AC


@pytest.mark.asyncio
async def test_ocr_absent_falls_back_to_context(tmp_path, db_conn):
    """OCR 不可用时行为不变：仍然按群聊上下文归类。"""
    outcome = await collect(make_image(3), root=tmp_path, text="这题 WA 到红温")
    assert outcome is not None
    assert outcome["category"] == memes.CATEGORY_WA_TLE


# ---------------- 截图拦截 ----------------

@pytest.mark.asyncio
async def test_long_ocr_text_rejected(tmp_path, db_conn, monkeypatch):
    """图内有效字符超过 35：按截图拒收，不落盘不记账。"""
    long_text = "这是一段很长的聊天记录内容用来模拟截图里的文字" * 3
    assert memes.ocr_text_length(long_text) > memes.OCR_MAX_CHARS
    monkeypatch.setattr(memes, "extract_image_text", lambda data: long_text)
    outcome = await collect(make_image(4), root=tmp_path)
    assert outcome is None
    assert database.count_meme_hashes(include_deleted=True) == 0
    assert list(tmp_path.rglob("*")) == [] or not any(p.is_file() for p in tmp_path.rglob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "#include <bits/stdc++.h>",
    "int main() { return 0; }",
    "std::vector<int> v;",
    "public static void main",
    "Traceback (most recent call last):",
    "Time Limit Exceeded",
    "Memory Limit Exceeded",
    "a; b; c;",          # 三个分号：符号堆叠兜底
    "if (x) { y; }",     # ; { } 三个符号：符号堆叠兜底
])
async def test_code_shot_rejected(tmp_path, db_conn, monkeypatch, text):
    """命中代码/题面特征（含 ; { } 堆叠）一律按题面截图拒收。"""
    monkeypatch.setattr(memes, "extract_image_text", lambda data: text)
    outcome = await collect(make_image(5), root=tmp_path)
    assert outcome is None
    assert database.count_meme_hashes(include_deleted=True) == 0


@pytest.mark.asyncio
async def test_short_ocr_text_still_collected(tmp_path, db_conn, monkeypatch):
    """少量文字（如表情包自带的「绝了」）不触发截图拦截。"""
    monkeypatch.setattr(memes, "extract_image_text", lambda data: "绝了")
    outcome = await collect(make_image(6), root=tmp_path, text="绝杀")
    assert outcome is not None


def test_inspect_ocr_text_boundaries():
    """字数/特征判定边界：35 字以内放行、36 字拒收、代码特征优先命中。"""
    assert memes.inspect_ocr_text("") == (True, "")
    assert memes.inspect_ocr_text("a" * 35)[0] is True
    assert memes.inspect_ocr_text("a" * 36) == (False, "图内文字过多，疑似截图")
    assert memes.inspect_ocr_text("  ") == (True, "")
    assert memes.inspect_ocr_text("int main")[1] == "疑似代码或题面截图"


# ---------------- dHash 相似去重 ----------------

def test_compute_dhash_is_stable_and_hex():
    """同一张图两次算出同一个 16 位十六进制串；损坏数据返回空串。"""
    data = make_image(7)
    first = memes.compute_dhash(data)
    assert first == memes.compute_dhash(data)
    assert len(first) == 16
    int(first, 16)  # 合法十六进制
    assert memes.compute_dhash(b"not an image") == ""


def test_dhash_distance_counts_bits():
    """汉明距离按比特算；空串/长度不一致返回最远距离。"""
    assert memes.dhash_distance("0000000000000000", "0000000000000000") == 0
    assert memes.dhash_distance("0000000000000000", "000000000000000f") == 4
    assert memes.dhash_distance("", "0000000000000000") == 64
    assert memes.dhash_distance("abc", "abcd") == 64


@pytest.mark.asyncio
async def test_similar_image_skipped_by_dhash(tmp_path, db_conn):
    """MD5 不同但 dHash 距离 <= 4：视为同一张图，第二次不再收录。"""
    original = make_flat_image(120)
    recolored = make_flat_image(121)  # 纯色图 dHash 全 0，等价于「重新压缩过」
    first = await collect(original, root=tmp_path, user_id="u1")
    second = await collect(recolored, root=tmp_path, user_id="u2")
    assert first is not None
    assert second is None
    assert database.count_meme_hashes() == 1


@pytest.mark.asyncio
async def test_different_image_not_blocked(tmp_path, db_conn):
    """dHash 距离大于阈值：不同图正常收录。"""
    first = await collect(make_image(8), root=tmp_path, user_id="u1")
    second = await collect(make_image(9), root=tmp_path, user_id="u2")
    assert first is not None and second is not None
    assert memes.dhash_distance(memes.compute_dhash(make_image(8)),
                                memes.compute_dhash(make_image(9))) > memes.DHASH_MAX_DISTANCE


@pytest.mark.asyncio
async def test_blacklisted_similar_image_skipped(tmp_path, db_conn):
    """黑名单（deleted=1）里的图，换个压缩再发一次也不回收。"""
    data = make_flat_image(120)
    await collect(data, root=tmp_path)
    md5 = database.list_meme_dhashes()[0]["md5"]
    database.mark_meme_hash_deleted(md5)
    again = await collect(make_flat_image(122), root=tmp_path, user_id="u2")
    assert again is None


# ---------------- 单人限流 ----------------

@pytest.mark.asyncio
async def test_single_user_rate_limited(tmp_path, db_conn):
    """同一用户 60 秒内只收 2 张，第 3 张静默跳过；换个人不受影响。"""
    outcomes = [await collect(make_image(10 + i), root=tmp_path, user_id="spammer")
                for i in range(3)]
    assert [item is not None for item in outcomes] == [True, True, False]
    other = await collect(make_image(20), root=tmp_path, user_id="honest")
    assert other is not None
    assert database.count_meme_hashes() == 3


@pytest.mark.asyncio
async def test_rate_limit_window_expires(tmp_path, db_conn, monkeypatch):
    """窗口滑过后额度恢复：靠注入 now 推进时间，不真等 60 秒。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(memes.time, "monotonic", lambda: clock["now"])
    first = await collect(make_image(30), root=tmp_path)
    second = await collect(make_image(31), root=tmp_path)
    blocked = await collect(make_image(32), root=tmp_path)
    assert [first is not None, second is not None, blocked is not None] == [True, True, False]
    clock["now"] += memes.COLLECT_USER_WINDOW_SECONDS + 1
    revived = await collect(make_image(33), root=tmp_path)
    assert revived is not None


@pytest.mark.asyncio
async def test_rejected_image_does_not_consume_quota(tmp_path, db_conn, monkeypatch):
    """被截图拦截的图不占额度：连续 3 张垃圾图后仍能正常收图。"""
    monkeypatch.setattr(memes, "extract_image_text", lambda data: "a" * 40)
    for i in range(3):
        assert await collect(make_image(40 + i), root=tmp_path) is None
    monkeypatch.setattr(memes, "extract_image_text", lambda data: "")
    assert await collect(make_image(50), root=tmp_path) is not None


@pytest.mark.asyncio
async def test_missing_user_id_skips_rate_limit(tmp_path, db_conn):
    """没拿到用户身份时不限流（宁可多收，也不误伤正常收图）。"""
    for i in range(4):
        assert await collect(make_image(60 + i), root=tmp_path, user_id="") is not None


# ---------------- 分类 FIFO 淘汰 ----------------

@pytest.mark.asyncio
async def test_category_cap_evicts_oldest(tmp_path, db_conn, monkeypatch):
    """分类上限 2：收第 3 张时删掉最旧的文件，库里只留 2 条现存记录。"""
    monkeypatch.setattr(config, "MEME_CATEGORY_CAP", 2)
    first = await collect(make_image(70), root=tmp_path, user_id="u1", text="摸鱼")
    second = await collect(make_image(71), root=tmp_path, user_id="u2", text="摸鱼")
    third = await collect(make_image(72), root=tmp_path, user_id="u3", text="摸鱼")
    assert first and second and third
    assert database.count_meme_hashes(category=memes.CATEGORY_SLACKING) == 2
    assert not (tmp_path / first["relative_path"]).exists()
    assert (tmp_path / second["relative_path"]).exists()
    assert (tmp_path / third["relative_path"]).exists()


@pytest.mark.asyncio
async def test_evicted_record_is_not_blacklist(tmp_path, db_conn, monkeypatch):
    """淘汰只腾容量：记录标记 cleaned 而非 deleted，同 MD5 再出现可以重新收录。"""
    monkeypatch.setattr(config, "MEME_CATEGORY_CAP", 1)
    payloads = [make_image(80), make_image(81)]
    first = await collect(payloads[0], root=tmp_path, user_id="u1", text="摸鱼")
    await collect(payloads[1], root=tmp_path, user_id="u2", text="摸鱼")
    record = database.get_meme_hash(first["md5"])
    assert record is not None
    assert record["cleaned"] is True
    assert record["deleted"] is False
    assert database.meme_hash_exists(first["md5"]) is False
    again = await collect(payloads[0], root=tmp_path, user_id="u3", text="摸鱼")
    assert again is not None
    assert database.get_meme_hash(first["md5"])["cleaned"] is False


@pytest.mark.asyncio
async def test_category_cap_zero_disables_eviction(tmp_path, db_conn, monkeypatch):
    """上限配成 0（关闭淘汰）：一张都不删。"""
    monkeypatch.setattr(config, "MEME_CATEGORY_CAP", 0)
    for i in range(3):
        assert await collect(make_image(90 + i), root=tmp_path, user_id=f"u{i}",
                            text="摸鱼") is not None
    assert database.count_meme_hashes(category=memes.CATEGORY_SLACKING) == 3


def test_config_category_cap_default(monkeypatch):
    """配置默认 100，脏值回退默认值。"""
    monkeypatch.delenv("ACM_MEME_CATEGORY_CAP", raising=False)
    assert config._positive_int("ACM_MEME_CATEGORY_CAP", 100) == 100
    monkeypatch.setenv("ACM_MEME_CATEGORY_CAP", "abc")
    assert config._positive_int("ACM_MEME_CATEGORY_CAP", 100) == 100
    monkeypatch.setenv("ACM_MEME_CATEGORY_CAP", "-5")
    assert config._positive_int("ACM_MEME_CATEGORY_CAP", 100) == 100
    monkeypatch.setenv("ACM_MEME_CATEGORY_CAP", "7")
    assert config._positive_int("ACM_MEME_CATEGORY_CAP", 100) == 7


# ---------------- 老库迁移 ----------------

def test_old_table_gets_dhash_and_cleaned_columns(monkeypatch):
    """老库（无 dhash/cleaned 列）启动时自动 ALTER，原有数据不丢。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    conn.execute(
        "CREATE TABLE meme_hashes (md5 TEXT PRIMARY KEY, category TEXT NOT NULL DEFAULT '',"
        " tags TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '',"
        " deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("INSERT INTO meme_hashes (md5, category, file_path)"
                 " VALUES ('legacy', 'ac', 'ac/legacy.png')")
    conn.commit()
    database.init_db()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(meme_hashes)")}
    assert {"dhash", "cleaned"} <= columns
    record = database.get_meme_hash("legacy")
    assert record is not None and record["dhash"] == "" and record["cleaned"] is False
    database.init_db()  # 幂等：再跑一次不报错
    conn.close()
