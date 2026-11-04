"""配置加载回归测试：空的 ACM_* 环境变量不能吃掉代码默认值。

真实故障：`.env` 里 `ACM_DB_PATH=`（空值）被 os.getenv 当作「已设置」，
DB_PATH 变成空串 → sqlite3.connect("") 打开临时库 → 每个连接都是全新空库
→ 建的表立刻消失 → 机器人启动后完全不回消息。
"""

import os

import config


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
