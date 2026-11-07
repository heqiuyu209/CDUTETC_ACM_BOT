"""配置加载回归测试：空的 ACM_* 环境变量不能吃掉代码默认值。

真实故障：`.env` 里 `ACM_DB_PATH=`（空值）被 os.getenv 当作「已设置」，
DB_PATH 变成空串 → sqlite3.connect("") 打开临时库 → 每个连接都是全新空库
→ 建的表立刻消失 → 机器人启动后完全不回消息。
"""


import pytest

from cdutetc import config


def test_drop_empty_env_removes_blank_acm_keys():
    environ = {"ACM_DB_PATH": "", "ACM_LLM_API_KEY": "  ", "ACM_LLM_MODEL": "deepseek-chat"}
    removed = config._drop_empty_env(environ)

    assert sorted(removed) == ["ACM_DB_PATH", "ACM_LLM_API_KEY"]
    assert "ACM_DB_PATH" not in environ
    assert "ACM_LLM_API_KEY" not in environ
    # 有值的键必须保留
    assert environ["ACM_LLM_MODEL"] == "deepseek-chat"


def test_drop_empty_env_ignores_non_acm_keys():
    """别的程序的环境变量不能被误删。"""
    environ = {"OTHER_VAR": "", "PATH": ""}
    removed = config._drop_empty_env(environ)

    assert removed == []
    assert "OTHER_VAR" in environ and "PATH" in environ


def test_db_path_is_not_empty():
    """DB_PATH 必须解析到真实路径，绝不能是空串（否则会连到临时库）。"""
    assert config.DB_PATH
    assert config.DB_PATH.strip()


def test_defaults_survive_blank_env():
    """带默认值的项在空环境下应回落到代码默认值。"""
    assert config.LLM_MODEL
    assert config.PUBLISH_CMD
    assert config.FETCH_TIMEOUT > 0
    assert config.LLM_MAX_INPUT_CHARS > 0


@pytest.mark.parametrize("multi,legacy,expected", [
    (None, None, (24.0, 1.0)),
    (None, "48", (48.0,)),
    ("", "48", (48.0,)),
    ("24,1,24,0.5", "48", (24.0, 1.0, 0.5)),
])
def test_reminder_offsets_preserve_default_and_explicit_legacy_config(monkeypatch, multi, legacy, expected):
    for name, value in (("ACM_REMIND_OFFSET_HOURS", multi), ("ACM_REMIND_BEFORE_HOURS", legacy)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert config._remind_offset_hours() == expected


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "1e100", "not-a-number", "24,nan"])
def test_invalid_reminder_offsets_are_rejected_before_scheduling(monkeypatch, value):
    monkeypatch.setenv("ACM_REMIND_OFFSET_HOURS", value)
    with pytest.raises(RuntimeError, match="提醒节点"):
        config._remind_offset_hours()


def test_env_sync_preserves_legacy_reminder_behavior_across_restarts(tmp_path, monkeypatch):
    current = tmp_path / ".env"
    template = tmp_path / ".env.example"
    current.write_text("ACM_REMIND_BEFORE_HOURS=48\n", encoding="utf-8")
    template.write_text(
        "ACM_REMIND_BEFORE_HOURS=24\nACM_REMIND_OFFSET_HOURS=24,1\nACM_QA_FAQ_LLM_ENABLED=0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE", current)
    monkeypatch.setattr(config, "ENV_EXAMPLE", template)
    assert config._sync_env_keys() == ["ACM_QA_FAQ_LLM_ENABLED"]
    assert config._sync_env_keys() == []
    saved = current.read_text(encoding="utf-8")
    assert "ACM_REMIND_BEFORE_HOURS=48" in saved
    assert "ACM_REMIND_OFFSET_HOURS" not in saved
