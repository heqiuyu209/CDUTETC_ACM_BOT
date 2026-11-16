"""单官方接入升级：旧环境、默认安装入口、平台标记与历史通知状态。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, database, main, memes
from cdutetc.clients.base import active_capabilities, active_protocol
from cdutetc.clients.qqbot import wrap_group_at_message, wrap_group_message
from cdutetc.utils import is_addressed_to_bot
from scripts import check_env


@pytest.mark.parametrize("legacy_protocol", ["", "onebot", "qqbot", "invalid-old-value"])
def test_existing_env_always_loads_official_credentials(tmp_path, legacy_protocol):
    """在独立配置副本中执行真实加载，避免触碰工作区配置与进程环境。"""
    root = Path(__file__).resolve().parents[1]
    package = tmp_path / "cdutetc"
    package.mkdir()
    shutil.copyfile(root / "cdutetc/config.py", package / "config.py")
    shutil.copyfile(root / ".env.example", tmp_path / ".env.example")
    original = (
        f"ACM_BOT_PROTOCOL={legacy_protocol}\n"
        "ACM_ONEBOT_WS_URL=ws://obsolete.invalid\nACM_ONEBOT_TOKEN=unused\n"
        "ACM_WS_IDLE_TIMEOUT_SECONDS=invalid-old-timeout\n"
        "ACM_QQBOT_APPID=offline-app\nACM_QQBOT_SECRET=offline-secret\n"
        "ACM_LLM_API_KEY=offline-key\nACM_ALLOWED_GROUPS=existing-openid\n"
        "ACM_DB_PATH=existing.db\n"
    )
    env_path = tmp_path / ".env"
    env_path.write_text(original, encoding="utf-8")
    script = (
        "import json, runpy; c=runpy.run_path('cdutetc/config.py'); "
        "c['check_config'](); "
        "assert c['QQBOT_APPID']=='offline-app'; "
        "assert c['QQBOT_SECRET']=='offline-secret'; "
        "assert c['DB_PATH']=='existing.db'; "
        "assert c['ENV_ALLOWED_GROUPS']=={'existing-openid'}; "
        "print(json.dumps({'protocol':c['BOT_PROTOCOL'], 'required':sorted(c['REQUIRED_CONFIG'])}))"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("ACM_")}
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert loaded == {"protocol": "qqbot", "required": [
        "ACM_LLM_API_KEY", "ACM_QQBOT_APPID", "ACM_QQBOT_SECRET",
    ]}
    assert env_path.read_text(encoding="utf-8").startswith(original)


@pytest.mark.parametrize("missing", ["ACM_QQBOT_APPID", "ACM_QQBOT_SECRET"])
def test_check_env_requires_official_credentials_even_with_old_transport(tmp_path, capsys, missing):
    values = {"ACM_BOT_PROTOCOL": "onebot", "ACM_ONEBOT_WS_URL": "ws://obsolete.invalid",
              "ACM_QQBOT_APPID": "offline-app", "ACM_QQBOT_SECRET": "offline-secret",
              "ACM_LLM_API_KEY": "offline-key"}
    values.pop(missing)
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()), encoding="utf-8")
    assert check_env.report_file(path) == 1
    output = capsys.readouterr().out
    assert f"结论：还差 {missing}" in output
    assert "offline-secret" not in output and "offline-key" not in output


def test_startup_rejects_missing_credentials_before_constructing_client(monkeypatch):
    monkeypatch.setattr(config, "REQUIRED_CONFIG", {
        "ACM_QQBOT_APPID": "", "ACM_QQBOT_SECRET": "", "ACM_LLM_API_KEY": "offline",
    })
    factory = AsyncMock()
    monkeypatch.setattr(main, "_build_client", factory)
    with pytest.raises(RuntimeError, match="ACM_QQBOT_APPID"):
        main.run_bot()
    factory.assert_not_called()


@pytest.mark.parametrize("client", [None, object(), SimpleNamespace(protocol="onebot")])
def test_missing_or_old_client_metadata_uses_official_defaults(client):
    capabilities = active_capabilities(client)
    assert active_protocol(client) == "qqbot"
    assert capabilities.supports_passive_reply and capabilities.supports_markdown
    assert not capabilities.accepts_group_id("123456789")
    assert capabilities.accepts_group_id("opaque-group-openid")


async def test_official_channel_state_survives_upgrade(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "existing.db"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"existing-openid"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    database.init_db()
    database.confirm_group_notification_channel("qqbot", "existing-openid", "markdown")
    database.init_db()
    assert await main.ACMBot(official_client)._send_group_result("existing-openid", "升级后通知")
    sent = official_client.api.post_group_message.await_args.kwargs
    assert sent["msg_type"] == 2 and sent["markdown"]["content"] == "升级后通知"
    assert sent["group_openid"] == "existing-openid" and "msg_id" not in sent


def test_platform_mentions_work_and_text_cannot_reenable_old_transport():
    payload = {"id": "event", "group_openid": "g1", "content": "<@30001> /帮助",
               "author": {"member_openid": "u1"}}
    assert not is_addressed_to_bot(wrap_group_message(payload))
    assert is_addressed_to_bot(wrap_group_at_message(payload))
    payload["content"] = "[CQ:at,qq=30001] [CQ:image,url=https://obsolete.invalid/a.png]"
    wrapped = wrap_group_message(payload)
    assert not is_addressed_to_bot(wrapped)
    assert memes.extract_image_refs(wrapped) == []


def test_official_attachment_keeps_signed_url_bytes():
    url = "https://example.invalid/a.png?token=abc%2Fdef&key=x%26y"
    wrapped = wrap_group_message({"attachments": [{"url": url, "content_type": "image/png"}]})
    assert memes.extract_image_refs(wrapped) == [{"url": url, "file": ""}]
