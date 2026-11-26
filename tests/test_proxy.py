from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import github_ctx, proxy
from app.config import get_settings
from app.db import session_scope
from app.models import LiveMessage, Message, ProxyLog


def H(t):
    return {"Authorization": f"Bearer {t}"}


def later(**kw):
    return datetime.now(UTC) + timedelta(**kw)


@pytest.fixture()
def ex(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "exaone_api_key", "exa-test-key")
    monkeypatch.setattr(st, "exaone_daily_limit", 90)
    monkeypatch.setattr(st, "proxy_all_members", False)
    monkeypatch.setattr(proxy, "MIN_CALL_GAP", 0)
    proxy._state.update(backoff_until=None, last_error="", last_error_at=None, last_call=0.0)
    github_ctx._cache.clear()
    calls = {"exaone": [], "answers": [], "status": [], "gh": [], "gh_files": {}}

    def http_post(url, *, headers, json, timeout=120):
        calls["exaone"].append({"url": url, "headers": headers, "json": json})
        if calls["status"]:
            code, hdrs = calls["status"].pop(0)
            return httpx.Response(code, headers=hdrs, json={"result_code": code, "description": "nope"})
        ans = calls["answers"].pop(0) if calls["answers"] else "\n\n메모 기준으로 좌표계는 Y-up 입니다."
        return httpx.Response(200, json={"choices": [{"message": {"content": ans, "reasoning_content": "생각…"}}]})

    def http_get(url, *, headers, timeout=20):
        calls["gh"].append(url)
        if url.endswith("/repos/acme/sim"):
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/trees/" in url:
            tree = [{"path": p, "type": "blob", "size": 100} for p in calls["gh_files"]]
            return httpx.Response(200, json={"tree": tree})
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            if path in calls["gh_files"]:
                return httpx.Response(200, text=calls["gh_files"][path])
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(proxy, "http_post", http_post)
    monkeypatch.setattr(github_ctx, "http_get", http_get)
    return calls


def logs():
    with session_scope() as db:
        return [(x.kind, x.status, x.called) for x in db.query(ProxyLog).order_by(ProxyLog.id)]


def test_settings(env, ex):
    c, t = env
    assert c.put("/api/proxy", headers=H(t["bob-ai"]), json={}).status_code == 403
    assert c.put("/api/proxy", headers=H(t["bob"]), json={"repos": ["not a repo"]}).status_code == 422
    r = c.put("/api/proxy", headers=H(t["bob"]), json={"repos": ["https://github.com/acme/sim/tree/dev/docs", "acme/sim"],
                                                       "inbox_after_minutes": 5})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["repos"] == ["acme/sim/docs@dev", "acme/sim"] and d["agent"] == "bob-gpt" and d["inbox_after_minutes"] == 5
    assert d["hub"]["key_configured"] and d["hub"]["available"] and "exa-test-key" not in r.text
    # repos 를 안 보내면 그대로 둔다
    r = c.put("/api/proxy", headers=H(t["bob"]), json={"live_after_seconds": 60})
    assert r.json()["repos"] == ["acme/sim/docs@dev", "acme/sim"]
    bob = next(m for m in c.get("/api/members", headers=H(t["alice"])).json() if m["name"] == "bob")
    assert bob["proxy"]["live_after_seconds"] == 60
    alice = next(m for m in c.get("/api/members", headers=H(t["alice"])).json() if m["name"] == "alice")
    assert alice["proxy"] is None
    assert c.delete("/api/proxy", headers=H(t["bob"])).status_code == 200
    assert c.get("/api/proxy", headers=H(t["bob"])).json()["configured"] is False


def test_parse_repo():
    assert github_ctx.parse_repo("acme/sim")["spec"] == "acme/sim"
    r = github_ctx.parse_repo("acme/sim/docs/api@release-1")
    assert (r["path"], r["branch"]) == ("docs/api", "release-1")
    assert github_ctx.parse_repo("https://github.com/acme/sim.git")["spec"] == "acme/sim"
    with pytest.raises(github_ctx.RepoSpecError):
        github_ctx.parse_repo("acme/sim/../etc")


def test_inbox_proxy_reply(env, ex):
    c, t = env
    c.put("/api/proxy", headers=H(t["bob"]), json={"repos": ["acme/sim"]})
    c.put("/api/notes/coords", headers=H(t["bob-ai"]), json={"content": "좌표계는 Y-up, 단위는 미터", "title": "좌표계"})
    ex["gh_files"].update({"README.md": "# Sim\n물류 시뮬레이터", "src/coords.py": "UP = 'Y'", "src/other.py": "x"})
    m = c.post("/api/messages", headers=H(t["alice-ai"]), json={"body": "coords 좌표계 뭐 써요?", "subject": "좌표", "to": "bob"}).json()
    assert proxy.process() == 0  # 아직 10분 안 됨
    assert proxy.process(later(minutes=11)) == 1
    req = ex["exaone"][0]
    assert req["url"] == "https://api.exaone.ai/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer exa-test-key"
    body = req["json"]
    assert body["model"] == "k-exaone_v2.5" and body["reasoning_effort"] == "low"
    user = body["messages"][1]["content"]
    assert "Y-up" in user and "물류 시뮬레이터" in user and "UP = 'Y'" in user and "src/other.py" in user
    assert "UP = 'Y'" in user and user.count("===== 파일 acme/sim/src/other.py") == 0 and "===== 파일 acme/sim/src/coords.py" in user
    with session_scope() as db:
        r = db.query(Message).filter(Message.parent_id == m["id"]).one()
        assert r.body.startswith(proxy.PREFIX + "메모 기준으로") and r.auto_via == "proxy" and r.sender_agent.name == "bob-gpt"
        assert r.wake_depth == 1
        assert db.get(Message, m["id"]).status == "open"  # 자동 답은 '답변됨'으로 바꾸지 않음
    assert logs() == [("inbox", "fired", True)]
    # 같은 글에 다시 답하지 않음, 대리 응답 글에는 상대 대리 응답도 답하지 않음
    c.put("/api/proxy", headers=H(t["alice"]), json={})
    assert proxy.process(later(minutes=30)) == 0
    assert len(ex["exaone"]) == 1


def test_inbox_skips_when_real_ai_replied(env, ex):
    c, t = env
    c.put("/api/proxy", headers=H(t["bob"]), json={})
    m = c.post("/api/messages", headers=H(t["alice"]), json={"body": "질문", "to": "bob"}).json()
    c.post(f"/api/messages/{m['id']}/replies", headers=H(t["bob-ai"]), json={"body": "진짜 답"})
    assert proxy.process(later(minutes=11)) == 0
    # 상대가 다시 물으면 그 글에는 대신 답함
    c.post(f"/api/messages/{m['id']}/replies", headers=H(t["alice"]), json={"body": "추가 질문"})
    assert proxy.process(later(minutes=11)) == 1
    # 깨우기 안 한 팀 전체 공지는 건너뜀
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "공지", "kind": "notice"})
    assert proxy.process(later(minutes=30)) == 0


def test_no_reply_and_errors(env, ex):
    c, t = env
    c.put("/api/proxy", headers=H(t["bob"]), json={})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "고마워요!", "to": "bob"})
    ex["answers"].append("NO_REPLY")
    assert proxy.process(later(minutes=11)) == 1
    assert logs() == [("inbox", "no_reply", True)]
    # 한도 초과(429): 기록을 지우고 잠시 쉼 → 나중에 다시
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "다른 질문", "subject": "q2", "to": "bob"})
    ex["status"].append((429, {"retry-after": "30"}))
    assert proxy.process(later(minutes=11)) == 1
    assert logs() == [("inbox", "no_reply", True)]
    assert proxy.process(later(minutes=11)) == 0  # 쉬는 중
    proxy._state["backoff_until"] = None
    assert proxy.process(later(minutes=11)) == 1
    assert logs()[-1] == ("inbox", "fired", True)
    # 키 만료(401): 실패로 남기고 30분 쉼, 설정에 오류 표시
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "세 번째", "subject": "q3", "to": "bob"})
    ex["status"].append((401, {}))
    proxy.process(later(minutes=11))
    assert logs()[-1] == ("inbox", "failed", False)
    d = c.get("/api/proxy", headers=H(t["bob"])).json()
    assert "30일" in d["last_error"] and not d["hub"]["available"]


def test_daily_limit(env, ex, monkeypatch):
    c, t = env
    monkeypatch.setattr(get_settings(), "exaone_daily_limit", 1)
    c.put("/api/proxy", headers=H(t["bob"]), json={})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "q1", "subject": "a", "to": "bob"})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "q2", "subject": "b", "to": "bob"})
    assert proxy.process(later(minutes=11)) == 1
    assert proxy.process(later(minutes=11)) == 0
    assert "한도" in c.get("/api/proxy", headers=H(t["bob"])).json()["hub"]["why_not"]


def test_no_key_no_work(env, ex, monkeypatch):
    c, t = env
    monkeypatch.setattr(get_settings(), "exaone_api_key", "")
    c.put("/api/proxy", headers=H(t["bob"]), json={})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "q", "to": "bob"})
    assert proxy.process(later(minutes=11)) == 0 and not ex["exaone"]


def test_live_proxy(env, ex):
    c, t = env
    c.put("/api/proxy", headers=H(t["bob"]), json={"live_after_seconds": 60})
    c.put("/api/proxy", headers=H(t["alice"]), json={"live_after_seconds": 60})
    s = c.post("/api/live", headers=H(t["alice-ai"]), json={"topic": "좌표 맞추기", "invite": ["bob"]}).json()
    sid = s["session_id"]
    c.post(f"/api/live/{sid}/messages", headers=H(t["alice-ai"]), json={"body": "bob, 좌표계 뭐 써요?", "to": ["bob"]})
    assert proxy.process(later(seconds=30)) == 0
    assert proxy.process(later(seconds=70)) == 1
    with session_scope() as db:
        msgs = db.query(LiveMessage).filter(LiveMessage.session_id == sid).order_by(LiveMessage.id).all()
        last = msgs[-1]
        assert last.body.startswith(proxy.PREFIX) and last.sender_agent.name == "bob-gpt"
        assert last.to_names == ["alice/alice-claude"]
    # alice 의 대리 응답은 대리 응답 말에 다시 답하지 않음, 같은 말에 두 번 답하지 않음
    assert proxy.process(later(seconds=200)) == 0
    assert [x[0] for x in logs()] == ["live"]
    user = ex["exaone"][0]["json"]["messages"][1]["content"]
    assert "좌표 맞추기" in user and "bob, 좌표계 뭐 써요?" in user


def test_live_waits_longer_when_real_ai_is_there(env, ex):
    c, t = env
    c.put("/api/proxy", headers=H(t["bob"]), json={"live_after_seconds": 60})
    sid = c.post("/api/live", headers=H(t["alice-ai"]), json={"topic": "t", "invite": ["bob"]}).json()["session_id"]
    c.post(f"/api/live/{sid}/join", headers=H(t["bob-ai"]))
    c.post(f"/api/live/{sid}/messages", headers=H(t["alice-ai"]), json={"body": "질문"})  # 두 사람 방: 지목 없어도 나에게
    c.get(f"/api/live/{sid}/wait", headers=H(t["bob-ai"]), params={"timeout": 0})  # 진짜 AI가 읽음
    assert proxy.process(later(seconds=70)) == 0  # 생각 중일 수 있으니 더 기다림
    assert proxy.process(later(seconds=190)) == 1


def test_github_problems_do_not_block(env, ex):
    c, t = env
    c.put("/api/proxy", headers=H(t["bob"]), json={"repos": ["acme/private"]})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "q", "to": "bob"})
    assert proxy.process(later(minutes=11)) == 1
    with session_scope() as db:
        row = db.query(ProxyLog).one()
        assert row.status == "fired" and "acme/private" in row.result
