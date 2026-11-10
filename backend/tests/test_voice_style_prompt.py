"""音色「真的换了一个人」：不认 voice 参数的供应商要用指令前缀。

线上现象：给男角色点试听，出来的还是女声。实测 `qwen-audio-3.1-tts-next`
根本不看 `input.voice` —— 传 Ethan / Cherry / 瞎写的值，基频都在 220~260Hz。
但它认自然语言指令：同一句话加「用低沉浑厚的成年男声朗读：」后基频 242Hz→164Hz，
时长只多 0.76s（说明指令不会被念出来）。

修复后实测：沉稳男声 128Hz / 清冷女声 215Hz，两条试听确实不同了。
"""

from __future__ import annotations

import asyncio

from app.agents import audio as audio_agent
from app.db.models import Voice
from app.providers.base import SpeechRequest
from app.providers import custom


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {"output": {"audio": {"url": "http://oss/a.wav", "duration": 1.0}},
                "request_id": "req_fake"}


class _FakeAudio:
    status_code = 200
    content = b"RIFFxxxxWAVEfmt "
    text = ""


class _FakeAsyncClient:
    """记录 POST 出去的 payload，音频下载直接返回假 wav。"""

    last_payload: dict | None = None

    def __init__(self, *args, **kwargs) -> None:  # noqa: ARG002
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, headers=None, json=None):  # noqa: ARG002
        _FakeAsyncClient.last_payload = json
        return _FakeResponse()

    async def get(self, url, timeout=None):  # noqa: ARG002
        return _FakeAudio()


def _voice(name: str, provider_voice_id: str, gender: str) -> Voice:
    return Voice(name=name, provider="qwen", provider_voice_id=provider_voice_id, gender=gender)


def test_style_prompt_by_preset_and_gender() -> None:
    assert "男声" in audio_agent.voice_style_prompt(_voice("沉稳男声", "Ethan", "male"))
    assert "女声" in audio_agent.voice_style_prompt(_voice("清冷女声", "Serena", "female"))
    # 认不出的自定义音色按性别兜底，不能返回空
    assert "男声" in audio_agent.voice_style_prompt(_voice("自定义", "whatever-voice", "male"))
    assert audio_agent.voice_style_prompt(None) == ""


def test_qwen_provider_prepends_style_prompt(monkeypatch) -> None:
    monkeypatch.setattr(custom.httpx, "AsyncClient", _FakeAsyncClient)
    provider = custom.QwenTtsProvider(
        model="qwen-audio-3.1-tts-next", api_key="k", base_url="http://gateway"
    )
    result = asyncio.run(
        provider.synthesize(
            SpeechRequest(text="你好。", voice_id="Ethan", style_prompt="用低沉浑厚的成年男声朗读：")
        )
    )
    assert result.ok, result.error_message
    payload = _FakeAsyncClient.last_payload or {}
    text = payload["input"]["text_prompt"]
    assert text == "用低沉浑厚的成年男声朗读：你好。", text
    # voice 仍然照发（别的部署可能认这个字段）
    assert payload["input"]["voice"] == "Ethan"


def test_style_prompt_is_not_spoken_when_empty(monkeypatch) -> None:
    """没有指令时不能凭空给正文加前缀（否则广告词都变了）。"""
    monkeypatch.setattr(custom.httpx, "AsyncClient", _FakeAsyncClient)
    provider = custom.QwenTtsProvider(api_key="k", base_url="http://gateway")
    asyncio.run(provider.synthesize(SpeechRequest(text="你好。", voice_id="Ethan")))
    payload = _FakeAsyncClient.last_payload or {}
    assert payload["input"]["text_prompt"] == "你好。"


def test_legacy_voice_ids_are_mapped_for_qwen(project: dict) -> None:
    from app.db.session import session_scope

    with session_scope() as session:
        voice = Voice(
            project_id=project["id"],
            name="沉稳男声",
            provider="minimax",
            provider_voice_id="male-qingnian",
            gender="male",
            preview_media_id="med_old",
        )
        session.add(voice)
        session.flush()
        changed = audio_agent.align_voice_to_provider(session, voice, "qwen")
        assert changed is True
        assert voice.provider_voice_id == "Ethan"
        assert voice.provider == "qwen"
        # 旧试听是用错的音色合成的，必须作废
        assert voice.preview_media_id is None
        session.rollback()
