"""官方富媒体表情包引擎离线测试：目录分类、降级、关键词冷却与 file_info 缓存。"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from cdutetc import config, database, memes

TZ = database.TZ


@pytest.fixture
def memes_root(tmp_path):
    """构造一份最小素材库：每个分类各一张图，另放一个非图片占位文件。"""
    for category in memes.CATEGORIES:
        directory = tmp_path / category
        directory.mkdir(parents=True)
        (directory / "1.png").write_bytes(b"fake-png")
    (tmp_path / memes.CATEGORY_AC / "README.md").write_text("占位", encoding="utf-8")
    return tmp_path


class FakeClock:
    def __init__(self, now: float = 500.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def db_conn(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


# ---------- 关键词匹配 ----------


@pytest.mark.parametrize("text,category", [
    ("这题 WA了", memes.CATEGORY_WA_TLE),
    ("又 TLE 了", memes.CATEGORY_WA_TLE),
    ("爆零了", memes.CATEGORY_WA_TLE),
    ("红温了", memes.CATEGORY_WA_TLE),
    ("AK 了", memes.CATEGORY_AC),
    ("太强了", memes.CATEGORY_AC),
    ("膜拜大佬", memes.CATEGORY_AC),
    ("摸鱼中", memes.CATEGORY_SLACKING),
    ("下班", memes.CATEGORY_SLACKING),
    ("早安", memes.CATEGORY_SLACKING),
    ("晚安", memes.CATEGORY_SLACKING),
    ("？？？", memes.CATEGORY_QUESTION),
    ("啥意思", memes.CATEGORY_QUESTION),
])
def test_match_category_keywords(text, category):
    assert memes.match_category(text) == category


@pytest.mark.parametrize("text", ["little", "make a cake", "今天天气不错", "", None])
def test_match_category_ignores_false_positive(text):
    """英文高频词按词边界匹配：little/make 不能命中 TLE/AK。"""
    assert memes.match_category(text) == ""


# ---------- 目录即分类 ----------


def test_list_memes_uses_category_dirs(memes_root):
    names = memes.list_memes(memes.CATEGORY_AC, root=memes_root)
    assert names == ["ac/1.png"]


def test_list_memes_missing_dir_returns_empty(tmp_path):
    assert memes.list_memes(memes.CATEGORY_AC, root=tmp_path / "nope") == []


def test_list_memes_empty_dir_returns_empty(tmp_path):
    (tmp_path / memes.CATEGORY_AC).mkdir(parents=True)
    assert memes.list_memes(memes.CATEGORY_AC, root=tmp_path) == []


def test_list_memes_unknown_category_returns_empty(memes_root):
    assert memes.list_memes("not-a-category", root=memes_root) == []


def test_pick_meme_returns_empty_without_assets(tmp_path):
    assert memes.pick_meme(memes.CATEGORY_AC, root=tmp_path) == ""


def test_pick_meme_returns_relative_path(memes_root):
    assert memes.pick_meme(memes.CATEGORY_AC, root=memes_root) == "ac/1.png"


# ---------- 公网地址与降级 ----------


def test_public_url_empty_when_base_missing(monkeypatch):
    monkeypatch.setattr(config, "MEME_BASE_URL", "")
    assert memes.public_url("ac/1.png") == ""


def test_public_url_joins_base(monkeypatch):
    monkeypatch.setattr(config, "MEME_BASE_URL", "https://cdn.example.com/memes/")
    assert memes.public_url("ac/1 号.png") == (
        "https://cdn.example.com/memes/ac/1%20%E5%8F%B7.png"
    )


def test_resolve_degrades_without_base_url(memes_root, monkeypatch):
    monkeypatch.setattr(config, "MEME_BASE_URL", "")
    engine = memes.MemeEngine(root=memes_root, rng=lambda: 0.0)
    assert engine.resolve(memes.CATEGORY_AC) is None


def test_resolve_degrades_without_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEME_BASE_URL", "https://cdn.example.com/memes")
    engine = memes.MemeEngine(root=tmp_path, rng=lambda: 0.0)
    assert engine.resolve(memes.CATEGORY_AC) is None


def test_resolve_returns_trigger(memes_root, monkeypatch):
    monkeypatch.setattr(config, "MEME_BASE_URL", "https://cdn.example.com/memes")
    engine = memes.MemeEngine(root=memes_root, rng=lambda: 0.0)
    trigger = engine.resolve(memes.CATEGORY_AC)
    assert trigger is not None
    assert trigger.relative_path == "ac/1.png"
    assert trigger.url == "https://cdn.example.com/memes/ac/1.png"


# ---------- 触发概率与每群冷却 ----------


def test_trigger_requires_keyword(memes_root):
    engine = memes.MemeEngine(root=memes_root, clock=FakeClock(), rng=lambda: 0.0)
    assert engine.should_trigger("g1", "今天天气不错") == ""


def test_trigger_hits_category(memes_root):
    engine = memes.MemeEngine(root=memes_root, clock=FakeClock(), rng=lambda: 0.0)
    assert engine.should_trigger("g1", "这题 WA了") == memes.CATEGORY_WA_TLE


def test_trigger_respects_probability(memes_root):
    engine = memes.MemeEngine(root=memes_root, clock=FakeClock(), rng=lambda: 0.99)
    assert engine.should_trigger("g1", "AK 了") == ""


def test_trigger_group_cooldown(memes_root):
    """每群 5 分钟冷却：冷却期内即使命中关键词也不再发。"""
    clock = FakeClock()
    engine = memes.MemeEngine(root=memes_root, clock=clock, rng=lambda: 0.0)
    assert engine.should_trigger("g1", "AK 了") == memes.CATEGORY_AC
    assert engine.should_trigger("g1", "AK 了") == ""
    clock.advance(memes.GROUP_COOLDOWN_SECONDS - 1)
    assert engine.should_trigger("g1", "WA了") == ""
    clock.advance(2)
    assert engine.should_trigger("g1", "WA了") == memes.CATEGORY_WA_TLE


def test_trigger_cooldown_is_per_group(memes_root):
    clock = FakeClock()
    engine = memes.MemeEngine(root=memes_root, clock=clock, rng=lambda: 0.0)
    assert engine.should_trigger("g1", "AK 了") == memes.CATEGORY_AC
    assert engine.should_trigger("g2", "AK 了") == memes.CATEGORY_AC


# ---------- file_info 缓存 ----------


def test_meme_cache_roundtrip(db_conn):
    assert database.get_meme_file_cache("g-openid", "ac/1.png") == ""
    assert database.save_meme_file_cache("g-openid", "ac/1.png", "FILE-INFO-1", ttl_seconds=600)
    assert database.get_meme_file_cache("g-openid", "ac/1.png") == "FILE-INFO-1"


def test_meme_cache_expires(db_conn):
    now = datetime.now(TZ)
    database.save_meme_file_cache("g-openid", "ac/1.png", "FILE-INFO-1",
                                  ttl_seconds=60, now=now)
    later = now + timedelta(seconds=61)
    assert database.get_meme_file_cache("g-openid", "ac/1.png", now=later) == ""


def test_meme_cache_is_per_group(db_conn):
    database.save_meme_file_cache("g1", "ac/1.png", "FILE-INFO-1", ttl_seconds=600)
    assert database.get_meme_file_cache("g2", "ac/1.png") == ""


def test_meme_cache_zero_ttl_uses_default(db_conn):
    database.save_meme_file_cache("g1", "ac/1.png", "FILE-INFO-1", ttl_seconds=0)
    assert database.get_meme_file_cache("g1", "ac/1.png") == "FILE-INFO-1"


def test_purge_meme_cache_removes_expired(db_conn):
    now = datetime.now(TZ)
    database.save_meme_file_cache("g1", "ac/1.png", "OLD", ttl_seconds=10, now=now)
    database.save_meme_file_cache("g1", "ac/2.png", "NEW", ttl_seconds=600, now=now)
    removed = database.purge_meme_file_cache(now=now + timedelta(seconds=20))
    assert removed == 1
    assert database.get_meme_file_cache("g1", "ac/2.png") == "NEW"


def test_meme_cache_table_in_reset_list():
    """缓存表必须进入业务数据重置清单，避免旧 file_info 残留。"""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "reset_business_data.py"
    spec = importlib.util.spec_from_file_location("reset_business_data", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "meme_file_cache" in module.BUSINESS_TABLES
