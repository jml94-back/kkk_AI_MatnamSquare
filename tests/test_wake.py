from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import wake
from app.config import get_settings
from app.db import session_scope
from app.models import Message, WakeJob

ROUTINE = "https://api.anthropic.com/v1/claude_code/routines/trig_01ABCdef/fire"


def H(t):
    return {"Authorization": f"Bearer {t}"}


def later(seconds=10):
    return datetime.now(UTC) + timedelta(seconds=seconds)


@pytest.fixture()
def fake(monkeypatch):
    calls = {"http": [], "llm": [], "answers": []}

    def http_post(url, *, headers, json, timeout=30):
        calls["http"].append({"url": url, "headers": headers, "json": json})
        return httpx.Response(200, json={"type": "routine_fire", "claude_code_session_url": "https://claude.ai/code/session_x"})

    def call_llm(provider, model, api_key, system, user):
        calls["llm"].append({"provider": provider, "model": model, "key": api_key, "system": system, "user": user})
        return calls["answers"].pop(0) if calls["answers"] else "자동 답변입니다"

    monkeypatch.setattr(wake, "http_post", http_post)
    monkeypatch.setattr(wake, "call_llm", call_llm)
    return calls


def jobs():
    with session_scope() as db:
        return [(j.reason, j.status, j.result) for j in db.query(WakeJob).order_by(WakeJob.id)]


def test_wake_settings(env):
    c, t = env
    body = {"mode": "claude_routine", "routine_url": ROUTINE, "routine_token": "sk-ant-oat01-secret"}
    assert c.put("/api/wake", headers=H(t["bob-ai"]), json=body).status_code == 403  # AI 토큰은 불가
    assert c.put("/api/wake", headers=H(t["bob"]), json={**body, "routine_url": "https://evil.example/fire"}).status_code == 422
    assert c.put("/api/wake", headers=H(t["bob"]), json={**body, "routine_token": None}).status_code == 422
    r = c.put("/api/wake", headers=H(t["bob"]), json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["agent"] == "bob-gpt" and d["has_secret"] and "sk-ant" not in r.text
    # 토큰 없이 다른 설정만 바꾸기
    r = c.put("/api/wake", headers=H(t["bob"]), json={"mode": "claude_routine", "max_per_hour": 3})
    assert r.status_code == 200 and r.json()["max_per_hour"] == 3
    # responder 로 바꾸려면 키 필요, openai 는 모델도 필요
    assert c.put("/api/wake", headers=H(t["bob"]), json={"mode": "responder", "provider": "openai", "model": "gpt-x"}).status_code == 422
    assert c.put("/api/wake", headers=H(t["bob"]), json={"mode": "responder", "provider": "openai", "api_key": "k"}).status_code == 422
    r = c.put("/api/wake", headers=H(t["alice"]), json={"mode": "responder", "provider": "anthropic", "api_key": "sk-ant-api"})
    assert r.json()["model"] == "claude-opus-5-5"
    assert c.get("/api/wake", headers=H(t["alice"])).json()["provider"] == "anthropic"
    assert c.delete("/api/wake", headers=H(t["alice"])).status_code == 200
    assert c.get("/api/wake", headers=H(t["alice"])).json() == {"configured": False}
    # 서버에 비밀 키가 없으면 저장 불가
    st = get_settings()
    old, st.secret_key = st.secret_key, ""
    try:
        assert c.put("/api/wake", headers=H(t["bob"]), json=body).status_code == 503
    finally:
        st.secret_key = old


def test_routine_fires_and_batches(env, fake):
    c, t = env
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "claude_routine", "routine_url": ROUTINE,
                                                   "routine_token": "sk-ant-oat01-secret"})
    m1 = c.post("/api/messages", headers=H(t["alice-ai"]), json={"body": "좌표계?", "subject": "좌표", "to": "bob"}).json()
    assert wake.process_due() == 0  # 잠깐 모았다가 보냄
    wake.process_due(later())
    assert len(fake["http"]) == 1
    call = fake["http"][0]
    assert call["url"] == ROUTINE and call["headers"]["Authorization"] == "Bearer sk-ant-oat01-secret"
    assert f"#{m1['id']}" in call["json"]["text"] and "좌표" in call["json"]["text"]
    assert jobs()[-1][1] == "fired"
    # 루틴 세션(bob-gpt)이 쓴 답글은 깊이 1을 물려받는다
    r = c.post(f"/api/messages/{m1['id']}/replies", headers=H(t["bob-ai"]), json={"body": "Y-up"}).json()
    with session_scope() as db:
        assert db.get(Message, r["id"]).wake_depth == 1
    # 1분 안에 온 글 두 개는 미뤄졌다가 한 번에
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "a", "to": "bob"})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "b", "to": "bob"})
    wake.process_due(later(20))
    assert len(fake["http"]) == 1
    wake.process_due(later(90))
    assert len(fake["http"]) == 2
    assert "2건" in fake["http"][1]["json"]["text"]
    assert [s for _, s, _ in jobs()][-2:] == ["fired", "merged"]
    # 자기 글·관심 없는 공지는 깨우지 않음
    c.post("/api/messages", headers=H(t["bob"]), json={"body": "내 메모"})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "공지", "kind": "notice"})
    assert len(jobs()) == 3


def test_responder_answers_with_notes(env, fake):
    c, t = env
    c.put("/api/wake", headers=H(t["alice"]), json={"mode": "responder", "provider": "anthropic", "api_key": "sk-ant-api",
                                                     "instructions": "존댓말로"})
    c.put("/api/notes/coords", headers=H(t["alice-ai"]), json={"content": "좌표계는 Y-up, 단위는 미터", "title": "좌표계"})
    m = c.post("/api/messages", headers=H(t["bob-ai"]), json={"body": "좌표계 단위가 뭐예요?", "to": "alice"}).json()
    wake.process_due(later())
    llm = fake["llm"][0]
    assert llm["key"] == "sk-ant-api" and llm["model"] == "claude-opus-5-5"
    assert "Y-up" in llm["user"] and "좌표계 단위" in llm["user"] and "존댓말로" in llm["system"]
    th = c.get(f"/api/messages/{m['id']}", headers=H(t["bob"])).json()
    assert th["status"] == "open"  # 자동 답글은 '답변됨'으로 바꾸지 않음
    rep = th["replies"][0]
    assert rep["from_agent"] == "alice-claude" and rep["auto"] == "responder" and rep["body"].startswith("[자동 응답]")
    # NO_REPLY 면 아무것도 쓰지 않음
    fake["answers"].append("NO_REPLY")
    c.post(f"/api/messages/{m['id']}/replies", headers=H(t["bob"]), json={"body": "감사합니다"})
    wake.process_due(later())
    th = c.get(f"/api/messages/{m['id']}", headers=H(t["bob"])).json()
    assert len(th["replies"]) == 2 and "NO_REPLY" in jobs()[-1][2]


def test_two_responders_stop(env, fake):
    """AI 둘이 서로 깨워도 연쇄 깊이 한도에서 멈춘다."""
    c, t = env
    c.put("/api/wake", headers=H(t["alice"]), json={"mode": "responder", "provider": "anthropic", "api_key": "a"})
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "responder", "provider": "openai", "model": "gpt-x", "api_key": "b"})
    m = c.post("/api/messages", headers=H(t["alice"]), json={"body": "질문", "to": "bob"}).json()
    for i in range(10):
        wake.process_due(later(10 + i))
    th = c.get(f"/api/messages/{m['id']}", headers=H(t["alice"])).json()
    autos = [r for r in th["replies"] if r["auto"]]
    assert len(autos) == get_settings().wake_max_chain
    with session_scope() as db:
        assert not db.query(WakeJob).filter(WakeJob.status == "pending").count()


def test_rate_limit_and_errors(env, fake, monkeypatch):
    c, t = env
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "responder", "provider": "gemini", "model": "gemini-x",
                                                   "api_key": "g", "max_per_hour": 1})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "1", "to": "bob"})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "2", "to": "bob"})
    wake.process_due(later())
    st = [s for _, s, _ in jobs()]
    assert st == ["fired", "skipped"]

    def boom(*a, **k):
        raise wake.WakeCallError("LLM 호출 거부 401: bad key", permanent=True)

    monkeypatch.setattr(wake, "call_llm", boom)
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "responder", "provider": "gemini", "max_per_hour": 10})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "3", "to": "bob"})
    wake.process_due(later())
    assert jobs()[-1][1] == "failed"
    assert "bad key" in c.get("/api/wake", headers=H(t["bob"])).json()["last_error"]
    assert c.post("/api/wake/test", headers=H(t["bob"])).json() == {"ok": False, "error": "LLM 호출 거부 401: bad key"}


def test_scheduled_mode(env, fake):
    """Cowork 예약 확인: 허브는 깨우지 않고, AI가 inbox 를 확인한 시각과 응답 방식을 보여 준다."""
    c, t = env
    r = c.put("/api/wake", headers=H(t["alice"]), json={"mode": "scheduled", "project": "물류 시뮬레이션"})
    assert r.status_code == 200, r.text
    assert r.json()["schedule"] == "hourly" and r.json()["last_checked_at"] is None
    assert c.put("/api/wake", headers=H(t["alice"]), json={"mode": "scheduled", "schedule": "every-minute"}).status_code == 422
    c.post("/api/messages", headers=H(t["bob"]), json={"body": "질문", "to": "alice"})
    wake.process_due(later())
    assert fake["http"] == [] and fake["llm"] == [] and jobs() == []
    members = {m["name"]: m for m in c.get("/api/members", headers=H(t["bob"])).json()}
    info = members["alice"]["ai_responds"]
    assert info["mode"] == "scheduled" and info["project"] == "물류 시뮬레이션" and info["agent"] == "alice-claude"
    assert members["bob"]["ai_responds"] is None
    # 사람이 inbox 를 봐도 기록 안 됨, AI가 보면 기록
    c.get("/api/inbox", headers=H(t["alice"]))
    assert c.get("/api/wake", headers=H(t["alice"])).json()["last_checked_at"] is None
    c.get("/api/inbox", headers=H(t["alice-ai"]))
    assert c.get("/api/wake", headers=H(t["alice"])).json()["last_checked_at"]
    assert c.post("/api/wake/test", headers=H(t["alice"])).json()["ok"] is True
