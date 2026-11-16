"""memes 从单文件拆成子包后，门面必须对既有调用方 100% 兼容。

覆盖范围：
  - 包结构：cdutetc/memes.py 已消失，cdutetc.memes 是包且由 Facade 类接管；
  - 公开面：__all__ 里每个符号都能解析到真实子模块定义（identity 相同）；
  - 读转发：门面取到的函数对象 __module__ 指向具体子模块，证明只是转发没复制；
  - 写转发：monkeypatch.setattr(memes, ...) 要作用到所有持有该名的子模块
    （拆包最容易踩的坑：只改到第一个模块，实际调用方仍用旧值）；
  - 未知属性仍然抛 AttributeError，运行时新增属性留在包自身；
  - 规模：拆分后每个子模块都低于 1000 行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdutetc import memes
from cdutetc.memes import collect, media, rules, screening, storage, trigger

CHILDREN = (rules, storage, screening, media, trigger, collect)


def test_package_replaces_the_old_single_file():
    """cdutetc.memes 现在是包，旧的 memes.py 不应再存在。"""
    init = Path(memes.__file__)
    assert init.name == "__init__.py"
    assert init.parent.name == "memes"
    assert not (init.parent.parent / "memes.py").exists()


def test_module_is_wrapped_by_facade_class():
    assert type(memes).__name__ == "_MemesFacade"


def test_public_surface_is_declared_without_duplicates():
    assert memes.__all__
    assert len(memes.__all__) == len(set(memes.__all__))
    for name in memes.__all__:
        assert hasattr(memes, name), f"{name} 未从任何子模块导出"


@pytest.mark.parametrize("name", sorted(memes.__all__))
def test_exported_symbol_resolves_to_child_definition(name):
    """门面返回的对象必须就是子模块里那个对象，而不是拷贝。"""
    owners = [mod for mod in CHILDREN if name in mod.__dict__]
    assert owners, f"{name} 在所有子模块里都找不到"
    assert getattr(memes, name) is getattr(owners[0], name)


@pytest.mark.parametrize("name", [
    "compute_dhash", "pick_meme", "public_url", "should_collect",
    "get_engine", "collect_one_image", "save_meme_file",
])
def test_callables_are_declared_in_child_modules(name):
    assert getattr(memes, name).__module__.startswith("cdutetc.memes.")


def test_unknown_attribute_still_raises_attribute_error():
    with pytest.raises(AttributeError):
        memes.this_symbol_never_existed


def test_constant_write_reaches_every_child_holding_it(monkeypatch):
    """拆分后同名常量在多个子模块各有一份，写门面必须全部覆盖。"""
    monkeypatch.setattr(memes, "TRIGGER_PROBABILITY", 1.0)
    assert rules.TRIGGER_PROBABILITY == 1.0
    assert trigger.TRIGGER_PROBABILITY == 1.0
    assert memes.TRIGGER_PROBABILITY == 1.0


def test_callable_patch_reaches_the_child_that_owns_it(monkeypatch):
    def fake_extract(_data):
        return ""

    monkeypatch.setattr(memes, "extract_image_text", fake_extract)
    assert screening.extract_image_text is fake_extract
    assert memes.extract_image_text is fake_extract


def test_memes_dir_patch_is_visible_to_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(memes, "MEMES_DIR", tmp_path)
    assert storage.MEMES_DIR == tmp_path
    (tmp_path / "ac").mkdir()
    (tmp_path / "ac" / "1.png").write_bytes(b"fixture")
    assert memes.meme_path("ac/1.png") == (tmp_path / "ac" / "1.png").resolve()
    assert memes.meme_path("../../etc/passwd") is None


def test_runtime_attribute_stays_on_package(monkeypatch):
    """新属性不属于任何子模块，落回包自身，且能被读回。"""
    monkeypatch.setattr(memes, "runtime_probe", "on", raising=False)
    assert memes.runtime_probe == "on"
    assert all("runtime_probe" not in mod.__dict__ for mod in CHILDREN)


def test_every_child_module_stays_under_thousand_lines():
    root = Path(memes.__file__).parent
    for path in sorted(root.glob("*.py")):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines < 1000, f"{path.name} 已增长到 {lines} 行"
