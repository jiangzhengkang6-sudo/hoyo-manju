"""氛围音 / BGM（分层方案）：台词逐句合成，垫乐单独一轨。

关键口径：
- 这一轨是「一次调用出一段环境声 + 音乐」，**不能**按台词逐句合成，
  否则每句都会从头起一段音乐，拼起来全是断点；
- 同一剧集只保留一条自动生成的 bgm 轨，重复生成必须替换而不是叠加；
- 计费按生成音频时长（秒），不是按字数。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents import audio as audio_agent
from app.core.errors import StateConflict
from app.db.models import AssetStatus, AudioTrack, Episode, Job, MediaAsset
from app.db.session import session_scope


def _bgm_tracks(episode_id: str) -> list[AudioTrack]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(AudioTrack).where(
                    AudioTrack.episode_id == episode_id, AudioTrack.kind == "bgm"
                )
            )
        )


def test_estimate_clamps_seconds_and_bills_by_second(
    client: TestClient, project: dict, episode: dict
) -> None:
    with session_scope() as session:
        quote = audio_agent.estimate_ambience(session, seconds=30)
        assert quote["billing_unit"] == "second"
        assert quote["seconds"] == 30
        assert quote["estimate_cents"] == 30          # mock 1 分/秒
        assert quote["can_generate"] is True

        assert audio_agent.estimate_ambience(session, seconds=1)["seconds"] == 3
        assert audio_agent.estimate_ambience(session, seconds=999)["seconds"] == 120


def test_generate_ambience_replaces_track_instead_of_stacking(
    client: TestClient, project: dict, episode: dict
) -> None:
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        first = audio_agent.generate_ambience(
            session, episode=ep, prompt="雨夜巷口，雨点打在铁皮棚上，低沉弦乐铺底", seconds=20
        )
        session.flush()

    assert first["duration_ms"] > 0
    tracks = _bgm_tracks(episode["id"])
    assert len(tracks) == 1, "第一次生成应挂出一条 bgm 轨"
    assert tracks[0].volume_db == audio_agent.AMBIENCE_DEFAULT_VOLUME_DB
    assert tracks[0].rights_declared is True
    with session_scope() as session:
        asset = session.get(MediaAsset, tracks[0].media_id)
        assert asset is not None
        assert asset.role == "bgm"
        assert asset.kind == "audio"
        assert asset.metadata_json.get("generated") is True

    # 再生成一次：轨道仍只有一条，旧素材被标记为 SUPERSEDED
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        second = audio_agent.generate_ambience(
            session, episode=ep, prompt="白天集市，轻快木吉他，人群嘈杂", seconds=15
        )
        session.flush()

    tracks = _bgm_tracks(episode["id"])
    assert len(tracks) == 1, "垫乐必须整条替换，两条会同时播放"
    assert tracks[0].media_id == second["media_id"]
    with session_scope() as session:
        old = session.get(MediaAsset, first["media_id"])
        assert old is not None
        assert old.status == AssetStatus.SUPERSEDED.value


def test_empty_prompt_is_rejected(client: TestClient, project: dict, episode: dict) -> None:
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        try:
            audio_agent.generate_ambience(session, episode=ep, prompt="   ", seconds=20)
        except StateConflict as exc:
            assert "描述" in str(exc)
        else:  # pragma: no cover - 明确失败比静默通过好
            raise AssertionError("空描述必须被拒绝")


def test_bgm_route_creates_job_with_reserve(
    client: TestClient, project: dict, episode: dict
) -> None:
    est = client.get(f"/api/v1/episodes/{episode['id']}/bgm/estimate", params={"seconds": 20})
    assert est.status_code == 200, est.text
    assert est.json()["estimate_cents"] == 20

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/bgm/generate",
        json={"prompt": "雨夜巷口，低沉弦乐铺底", "seconds": 20, "volume_db": -14},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "BGM_GENERATION"
    assert body["estimate"]["billing_unit"] == "second"

    with session_scope() as session:
        job = session.get(Job, body["id"])
        assert job is not None
        assert job.cost_reserved_cents == body["estimate"]["estimate_cents"]


def _seed_shots(episode_id: str, durations_ms: list[int]) -> None:
    from app.db.models import Shot

    with session_scope() as session:
        for no, duration in enumerate(durations_ms, start=1):
            session.add(
                Shot(
                    episode_id=episode_id,
                    shot_no=no,
                    duration_ms=duration,
                    shot_json={"action": f"动作{no}", "image_prompt": "画面"},
                    stage="CONFIRMED",
                )
            )
        session.flush()


def test_ambience_can_target_single_shot(
    client: TestClient, project: dict, episode: dict
) -> None:
    """单镜头垫乐：起点跟着镜头走、长度按镜头裁剪，且不覆盖整集。"""
    _seed_shots(episode["id"], [4000, 6000])

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        quote = audio_agent.estimate_ambience(session, episode=ep, shot_no=2)
        assert quote["window"]["start_ms"] == 4000
        assert quote["window"]["duration_ms"] == 6000
        assert quote["seconds"] == 6, "按镜头长度取整到秒"

        # 提示词够长 → 供应商会出超过 6 秒的音频，必须裁到镜头长度
        result = audio_agent.generate_ambience(
            session,
            episode=ep,
            prompt="雨点打在铁皮棚上，远处闷雷，低沉的弦乐缓慢铺底，没有人声，持续一段时间",
            seconds=30,
            shot_no=2,
        )
        session.flush()

    assert result["window"]["shot_no"] == 2
    assert result["duration_ms"] <= 6000
    assert result["generated_ms"] >= result["duration_ms"]

    with session_scope() as session:
        tracks = list(
            session.scalars(
                select(AudioTrack).where(
                    AudioTrack.episode_id == episode["id"], AudioTrack.kind == "ambience"
                )
            )
        )
        assert len(tracks) == 1
        assert tracks[0].shot_id == result["window"]["shot_id"]
        assert tracks[0].start_ms == 4000
        assert tracks[0].end_ms <= 10000

    # 换一个镜头：两条并存（各自覆盖自己的时间段）
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        audio_agent.generate_ambience(
            session, episode=ep, prompt="夜风穿过走廊，轻微回声", seconds=10, shot_no=1
        )
        session.flush()
    with session_scope() as session:
        tracks = list(
            session.scalars(
                select(AudioTrack).where(
                    AudioTrack.episode_id == episode["id"], AudioTrack.kind == "ambience"
                )
            )
        )
        assert len(tracks) == 2
        assert {t.shot_id for t in tracks} == {t.shot_id for t in tracks}
        assert sorted(t.start_ms for t in tracks) == [0, 4000]

    # 同一镜头再生成：只替换它自己，不新增
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        audio_agent.generate_ambience(
            session, episode=ep, prompt="雨声更密，低音更沉", seconds=10, shot_no=2
        )
        session.flush()
    with session_scope() as session:
        tracks = list(
            session.scalars(
                select(AudioTrack).where(
                    AudioTrack.episode_id == episode["id"], AudioTrack.kind == "ambience"
                )
            )
        )
        assert len(tracks) == 2, "同镜头重生成必须替换，不能叠加"


def test_ambience_can_cover_multiple_shots(
    client: TestClient, project: dict, episode: dict
) -> None:
    """一段垫乐跨多个镜头共用：起点取首镜、长度铺到末镜结束。"""
    _seed_shots(episode["id"], [4000, 6000, 5000])

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        quote = audio_agent.estimate_ambience(
            session, episode=ep, shot_from=2, shot_to=3
        )
        assert quote["window"]["shot_from"] == 2
        assert quote["window"]["shot_to"] == 3
        assert quote["window"]["shot_nos"] == [2, 3]
        assert quote["window"]["start_ms"] == 4000
        assert quote["window"]["duration_ms"] == 11000, "4+6 两镜共 10 秒，到第 3 镜结束"
        assert quote["seconds"] == 11

        result = audio_agent.generate_ambience(
            session,
            episode=ep,
            prompt="王座厅回声，远处号角，低沉弦乐铺底，没有人声",
            shot_from=2,
            shot_to=3,
        )
        session.flush()

    assert result["window"]["shot_nos"] == [2, 3]
    assert result["window"]["duration_ms"] == 11000
    with session_scope() as session:
        track = session.scalar(
            select(AudioTrack).where(
                AudioTrack.episode_id == episode["id"],
                AudioTrack.kind == "ambience",
            )
        )
        assert track is not None
        assert track.start_ms == 4000, "区间垫乐从首镜起点开始"
        assert track.end_ms <= 15000, "不能压到区间之外的镜头"
        asset = session.get(MediaAsset, track.media_id)
        assert asset.metadata_json["shot_from"] == 2
        assert asset.metadata_json["shot_to"] == 3


def test_suggest_ambience_reads_shot_content(
    client: TestClient, project: dict, episode: dict
) -> None:
    """氛围描述草稿要跟着镜头走：场景、音效、情绪都从分镜里取。"""
    from app.db.models import Shot

    with session_scope() as session:
        session.add(
            Shot(
                episode_id=episode["id"],
                shot_no=1,
                duration_ms=4000,
                stage="CONFIRMED",
                shot_json={
                    "action": "推门而入",
                    "image_prompt": (
                        "竖屏9:16，日系厚涂动画风，雨夜巷口，近景构图，"
                        "青年撑伞站在积水里"
                    ),
                    "sfx": ["雨声", "闷雷"],
                    "emotion": "压抑、疲惫",
                },
            )
        )
        session.flush()

    resp = client.get(
        f"/api/v1/episodes/{episode['id']}/bgm/suggest", params={"shot_from": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "雨夜巷口" in body["prompt"]
    assert "雨声" in body["prompt"] and "闷雷" in body["prompt"]
    assert "压抑" in body["prompt"]
    assert "没有人声" in body["prompt"]
    assert "近景" not in body["prompt"], "构图词不该进氛围描述"
    assert body["sources"]["sfx"] == ["雨声", "闷雷"]
    assert body["sources"]["shots"] == 1
