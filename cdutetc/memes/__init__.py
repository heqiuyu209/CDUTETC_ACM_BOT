"""QQ 官方富媒体表情包引擎（Facade）：对外保持 ``cdutetc.memes`` 全部原有符号。

链路说明（官方协议发图两段式）：
  1) 把公网图片 URL 提交 ``/v2/groups/{group_openid}/files``（file_type=1）换 ``file_info``；
  2) 用 ``msg_type=7`` 的被动回复把 ``file_info`` 发进群。

实现已按职责拆到本包各子模块：
  - :mod:`cdutetc.memes.rules`     分类目录/关键词规则/全部阈值常量（单一事实源）
  - :mod:`cdutetc.memes.storage`   素材目录与命名、MD5/感知哈希去重、淘汰与限流台账
  - :mod:`cdutetc.memes.screening` OCR 文本判定、尺寸/体积准入、静态图瘦身
  - :mod:`cdutetc.memes.media`     CQ 清理、图片引用解析、下载与公网地址拼接
  - :mod:`cdutetc.memes.trigger`   关键词分类、场景时机、概率冷却与素材挑选
  - :mod:`cdutetc.memes.collect`   自动收图编排（下载→准入→去重→归类→落盘→记账）

本门面通过模块级 ``__getattr__``/``__setattr__`` 把属性读写（含测试里的 monkeypatch）
统一转发到真实实现所在子模块，因此 ``memes.get_engine()``、
``monkeypatch.setattr(memes, "MEMES_DIR", tmp)`` 等既有用法语义不变。
"""

import sys
import types

from cdutetc.memes import collect as _collect
from cdutetc.memes import media as _media
from cdutetc.memes import rules as _rules
from cdutetc.memes import screening as _screening
from cdutetc.memes import storage as _storage
from cdutetc.memes import trigger as _trigger

_CHILDREN = (_rules, _storage, _screening, _media, _trigger, _collect)

__all__ = [
    "CATEGORIES",
    "CATEGORY_AC",
    "CATEGORY_INTERRUPT",
    "CATEGORY_QUESTION",
    "CATEGORY_SLACKING",
    "CATEGORY_WA_TLE",
    "COLLECT_CONTEXT_LINES",
    "COLLECT_DEFAULT_CATEGORY",
    "COLLECT_JPEG_QUALITY",
    "COLLECT_MAX_ASPECT",
    "COLLECT_MAX_BYTES",
    "COLLECT_MAX_WIDTH",
    "COLLECT_MIN_ASPECT",
    "COLLECT_MIN_SIDE",
    "COLLECT_USER_MAX_IMAGES",
    "COLLECT_USER_WINDOW_SECONDS",
    "DHASH_MAX_DISTANCE",
    "DHASH_SIZE",
    "GIF_SUFFIX",
    "GROUP_COOLDOWN_SECONDS",
    "IMAGE_SUFFIXES",
    "LATE_NIGHT_KEYWORDS",
    "LATE_NIGHT_WINDOWS",
    "MEAL_KEYWORDS",
    "MEAL_WINDOWS",
    "MEMES_DIR",
    "OCR_CODE_HINTS",
    "OCR_CODE_SYMBOL_MIN",
    "OCR_MAX_CHARS",
    "QUESTION_MAX_CHARS",
    "QUESTION_WINDOW",
    "RECENT_SENT_LIMIT",
    "SCENE_COOLDOWN_SECONDS",
    "TRIGGER_PROBABILITY",
    "MemeEngine",
    "MemeTrigger",
    "classify_collect",
    "collect_from_message",
    "collect_one_image",
    "compute_dhash",
    "delete_last_sent",
    "delete_meme_file",
    "dhash_distance",
    "enforce_category_cap",
    "extract_image_refs",
    "extract_image_text",
    "fetch_image_bytes",
    "file_suffix",
    "filename_tag",
    "find_similar_meme",
    "get_engine",
    "image_md5",
    "inspect_ocr_text",
    "inspect_static_image",
    "is_short_question",
    "is_within",
    "list_memes",
    "looks_like_code_shot",
    "match_category",
    "meme_filename",
    "meme_path",
    "normalize_image_url",
    "ocr_text_length",
    "pick_meme",
    "public_url",
    "reset_collect_limits",
    "reset_engine",
    "sanitize_tag",
    "save_meme_file",
    "should_collect",
    "shrink_static_image",
    "slacking_scene",
    "store_meme_bytes",
    "strip_cq_codes",
    "text_hits_tag",
]

_LOCAL = {
    "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__path__",
    "__builtins__", "_CHILDREN", "_LOCAL", "_MemesFacade", "__all__",
}


class _MemesFacade(types.ModuleType):
    """表情包门面：属性读写转发到拆分后的真实子模块。"""

    def __getattr__(self, name):
        for mod in _CHILDREN:
            if name in mod.__dict__:
                return mod.__dict__[name]
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        if name in _LOCAL:
            self.__dict__[name] = value
            return
        hit = False
        # 拆分后每个子模块各自持有一份同名绑定（from ... import X），而单文件时
        # 只有一个全局名。所以写操作要写到所有持有该名的子模块，否则
        # monkeypatch.setattr(memes, "TRIGGER_PROBABILITY", ...) 只改到第一个模块，
        # trigger 里的判定仍用旧值——这正是拆包最容易埋的坑。
        for mod in _CHILDREN:
            if name in mod.__dict__:
                setattr(mod, name, value)
                hit = True
        if not hit:
            self.__dict__[name] = value

    def __dir__(self):
        names = set(self.__dict__)
        for mod in _CHILDREN:
            names |= set(mod.__dict__)
        return sorted(n for n in names if not n.startswith("__"))


sys.modules[__name__].__class__ = _MemesFacade
