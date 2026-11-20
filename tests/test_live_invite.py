"""실시간 세션을 열거나 팀원을 부르면 그 팀원의 AI를 허브가 바로 깨운다."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_events import chatgpt, subscribe  # noqa: F401 (fixture)

from app import wake
from app.db import session_scope
from app.models import WakeJob

ROUTINE = "https://api.anthropic.com/v1/claude_code/routines/trig_01ABCdef/fire"


def H(t):
    return {"Authorization": f"Bearer {t}"}


def soon(seconds=2):
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.fixture()
def fired(monkeypatch):
    calls = []

    def http_post(url, *, headers, json, timeout=30):
        calls.append(json["text"])
        return httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/session_x"})

    monkeypatch.setattr(wake, "http_post", http_post)
    return calls


def routine(c, token):
    r = c.put("/api/wake", headers=H(token), json={"mode": "claude_routine", "routine_url": ROUTINE,
                                                   "routine_token": "sk-ant-oat01-secret"})
    assert r.status_code == 200, r.text


def jobs():
    with session_scope() as db:
        return [(j.reason, j.status) for j in db.query(WakeJob).order_by(WakeJob.id)]


def test_start_session_wakes_invited_not_creator(env, fired):
    c, t = env
    routine(c, t["bob"])
    routine(c, t["alice"])  # 연 사람(alice)은 깨우지 않아야 함
    r = c.post("/api/live", headers=H(t["alice-ai"]), json={"topic": "좌표계", "invite": ["bob"]})
    assert r.status_code == 201, r.text
    s = r.json()
    sid = s["session_id"]
    assert s["invite_wake"] == [{"name": "bob", "auto": True, "woken_how": "Claude 루틴을 바로 깨움 (몇 분 안)"}]
    assert "전해 달라" in s["next_step"] and s["notified"] == ["bob"]
    # 활동 시간대와 상관없이 바로 깨움
    wake.process_due(soon())
    assert len(fired) == 1
    assert f"세션 #{sid}" in fired[0] and "join_live_session" in fired[0] and "'좌표계'" in fired[0]
    assert jobs() == [("live_invite", "fired")]

    # 깨어난 AI 의 check_in 에 초대가 보이고, 들어가면 사라진다
    ci = c.get("/api/check-in", headers=H(t["bob-ai"])).json()
    assert ci["has_work"] and ci["live_invites"][0]["session_id"] == sid
    assert f"join_live_session(session_id={sid})" in ci["todo"][0]
    assert any("[실시간 세션 초대]" in m["subject"] for m in c.get("/api/inbox", headers=H(t["bob"])).json())
    assert c.post(f"/api/live/{sid}/join", headers=H(t["bob-ai"])).status_code == 200
    ci = c.get("/api/check-in", headers=H(t["bob-ai"])).json()
    assert ci["live_invites"] == [] and ci["has_work"] is False  # 초대 글도 읽음 처리됨
    # 만든 쪽의 AI 는 자기 세션 초대를 받지 않는다
    assert c.get("/api/check-in", headers=H(t["alice-ai"])).json()["live_invites"] == []


def test_session_closed_before_wake_is_skipped(env, fired):
    c, t = env
    routine(c, t["bob"])
    sid = c.post("/api/live", headers=H(t["alice-ai"]), json={"topic": "x", "invite": ["bob"]}).json()["session_id"]
    c.post(f"/api/live/{sid}/close", headers=H(t["alice-ai"]), json={})
    wake.process_due(soon())
    assert fired == [] and jobs() == [("live_invite", "skipped")]
    assert c.get("/api/check-in", headers=H(t["bob-ai"])).json()["live_invites"] == []


def test_mention_or_invite_wakes_once(env, fired):
    c, t = env
    routine(c, t["bob"])
    # invite 없이 연 방: 아무도 깨우지 않음
    s = c.post("/api/live", headers=H(t["alice-ai"]), json={"topic": "열린 방"}).json()
    sid = s["session_id"]
    assert s["invite_wake"] == [] and "invite_live_session" in s["next_step"]
    wake.process_due(soon())
    assert fired == []
    # 아직 안 들어온 팀원을 지목하면 그 AI를 부른다 (한 번만)
    r = c.post(f"/api/live/{sid}/messages", headers=H(t["alice-ai"]),
               json={"body": "bob, Y-up 맞나요?", "to": ["bob"], "wait_for_reply": False})
    assert r.status_code == 201, r.text
    assert r.json()["sent"]["invite_wake"][0]["name"] == "bob"
    c.post(f"/api/live/{sid}/messages", headers=H(t["alice-ai"]),
           json={"body": "bob?", "to": ["bob"], "wait_for_reply": False})
    assert c.post(f"/api/live/{sid}/invite", headers=H(t["alice-ai"]), json={"members": ["bob"]}).json()["invite_wake"] == []
    assert c.post(f"/api/live/{sid}/messages", headers=H(t["alice-ai"]),
                  json={"body": "?", "to": ["nobody"], "wait_for_reply": False}).status_code == 422
    wake.process_due(soon(70))
    assert len(fired) == 1 and jobs() == [("live_invite", "fired")]
    # 들어오면 나를 지목한 말이 history 에 to_me 로 보인다
    j = c.post(f"/api/live/{sid}/join", headers=H(t["bob-ai"])).json()
    assert any(m["to_me"] for m in j["history"])


def test_invite_endpoint_adds_to_invite_list(env, fired):
    c, t = env
    routine(c, t["bob"])
    sid = c.post("/api/live", headers=H(t["alice"]), json={"topic": "a", "invite": ["alice"]}).json()["session_id"]
    assert jobs() == []
    r = c.post(f"/api/live/{sid}/invite", headers=H(t["alice"]), json={"members": ["bob/bob-gpt"]})
    assert r.status_code == 200, r.text
    assert "bob" in r.json()["invited"] and r.json()["invite_wake"][0]["auto"]
    wake.process_due(soon())
    assert len(fired) == 1
    assert c.post(f"/api/live/{sid}/join", headers=H(t["bob-ai"])).status_code == 200


def test_scheduled_member_learns_via_check_in(env, fired):
    c, t = env
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "scheduled", "schedule": "tiers", "client": "codex"})
    s = c.post("/api/live", headers=H(t["alice-ai"]), json={"topic": "b", "invite": ["bob"]}).json()
    w = s["invite_wake"][0]
    assert w["auto"] is False and "Codex" in w["woken_how"] and "bob" in s["next_step"]
    wake.process_due(soon())
    assert fired == [] and jobs() == []
    ci = c.get("/api/check-in", headers=H(t["bob-ai"])).json()
    assert ci["has_work"] and ci["live_invites"][0]["session_id"] == s["session_id"]


def test_chatgpt_gets_live_invite_event(env, chatgpt):  # noqa: F811
    c, t = env
    subscribe(c, t["bob-ai"])  # 이미 쓰던 '허브 새 글' 구독으로 받는다
    sid = c.post("/api/live", headers=H(t["alice-ai"]), json={"topic": "c", "invite": ["bob"]}).json()["session_id"]
    wake.process_due(soon())
    ev = [g["body"] for g in chatgpt["got"] if g["body"].get("name") == "message.received"]
    assert ev and ev[-1]["data"]["reason"] == "live_invite" and ev[-1]["data"]["session_id"] == sid
    assert f"join_live_session(session_id={sid})" in ev[-1]["data"]["next_step"]
