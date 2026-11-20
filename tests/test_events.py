"""ChatGPT 연결: MCP Events 구독·전달, ChatGPT OAuth(CIMD)·읽기 전용 표시, ChatGPT 예약 작업 방식."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_hub import _pkce, mcp_call

from app import events, schedule, wake
from app.config import get_settings
from app.db import session_scope
from app.models import Appointment, EventSubscription, WakeJob

CB = "https://hooks.chatgpt.com/mcp/events/abc"
SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()


def H(t):
    return {"Authorization": f"Bearer {t}"}


def later(seconds=10):
    return datetime.now(UTC) + timedelta(seconds=seconds)


def check_sig(secret, headers, body: bytes) -> bool:
    key = base64.b64decode(secret[len("whsec_"):])
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    want = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, headers["webhook-signature"])


@pytest.fixture()
def chatgpt(monkeypatch):
    """ChatGPT 쪽 콜백을 흉내 낸다: 서명을 확인하고, 확인 요청에는 challenge 를 되돌려준다."""
    st = {"got": [], "status": 200, "echo": True}

    def http_post(url, *, headers, content):
        assert check_sig(SECRET, headers, content)
        body = json.loads(content)
        st["got"].append({"url": url, "headers": headers, "body": body})
        if body.get("type") == "verification":
            return httpx.Response(200, json={"challenge": body["challenge"] if st["echo"] else "nope"})
        return httpx.Response(st["status"])

    monkeypatch.setattr(events, "http_post", http_post)
    monkeypatch.setattr(events, "check_callback_url", lambda url: None)
    return st


def rpc(c, token, method, params=None):
    r = mcp_call(c, token, method, params)
    assert r.status_code == 200, r.text
    return r.json()


def subscribe(c, token, name="message.received", **extra):
    return rpc(c, token, "events/subscribe", {"name": name, "arguments": {},
                                              "delivery": {"mode": "webhook", "url": CB, "secret": SECRET},
                                              "cursor": None, **extra})


def test_initialize_advertises_events_and_read_only_tools(env):
    c, t = env
    r = rpc(c, t["bob-ai"], "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "openai-mcp", "version": "1"}})
    caps = r["result"]["capabilities"]
    assert caps["events"] == {} and "tools" in caps
    tools = {x["name"]: x for x in rpc(c, t["bob-ai"], "tools/list")["result"]["tools"]}
    assert tools["read_inbox"]["annotations"]["readOnlyHint"] is True
    assert tools["check_in"]["annotations"]["readOnlyHint"] is True
    assert not (tools["reply"].get("annotations") or {}).get("readOnlyHint")  # 쓰기는 확인을 받게 둔다

    lst = rpc(c, t["bob-ai"], "events/list")["result"]["events"]
    names = {e["name"] for e in lst}
    assert names == {"message.received", "appointment.started"}
    for e in lst:
        assert e["delivery"] == ["webhook"] and e["inputSchema"]["type"] == "object" and "payloadSchema" in e


def test_subscribe_verifies_and_delivers_signed_event(env, chatgpt):
    c, t = env
    r = subscribe(c, t["bob-ai"])["result"]
    assert r["id"].startswith("sub_") and r["cursor"] is None and r["truncated"] is False and r["refreshBefore"]
    v = chatgpt["got"][0]
    assert v["body"]["type"] == "verification" and v["headers"]["X-MCP-Subscription-Id"] == r["id"]
    # 같은 정체성으로 다시 구독 → 같은 id, 확인은 캐시되어 다시 안 함
    assert subscribe(c, t["bob-ai"], ttlMs=3_600_000)["result"]["id"] == r["id"]
    assert len(chatgpt["got"]) == 1
    # 구독하면 깨우기 설정이 자동으로 'ChatGPT 이벤트' 방식이 된다
    w = c.get("/api/wake", headers=H(t["bob"])).json()
    assert w["mode"] == "chatgpt_events" and w["subscriptions"][0]["active"]
    assert "whsec" not in json.dumps(w)

    m = c.post("/api/messages", headers=H(t["alice-ai"]), json={"body": "좌표계?", "subject": "좌표", "to": "bob"}).json()
    wake.process_due(later())
    ev = chatgpt["got"][-1]
    assert ev["body"]["name"] == "message.received" and ev["body"]["eventId"] == ev["headers"]["webhook-id"]
    d = ev["body"]["data"]
    assert d["message_id"] == m["id"] and d["reason"] == "direct" and d["from"] == "alice/alice-claude"
    assert "좌표계?" not in json.dumps(d, ensure_ascii=False)  # 본문은 보내지 않고 허브 도구로 읽게 한다
    with session_scope() as db:
        assert db.query(WakeJob).one().status == "fired"

    # 깨어난 ChatGPT 가 쓴 답글은 연쇄 깊이를 물려받는다
    rep = c.post(f"/api/messages/{m['id']}/replies", headers=H(t["bob-ai"]), json={"body": "답"}).json()
    from app.models import Message
    with session_scope() as db:
        assert db.get(Message, rep["id"]).wake_depth == 1

    # 구독 해지 → 더 이상 보내지 않음
    assert rpc(c, t["bob-ai"], "events/unsubscribe", {"name": "message.received", "arguments": {},
                                                      "delivery": {"mode": "webhook", "url": CB}})["result"] == {}
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "또", "to": "bob"})
    n = len(chatgpt["got"])
    wake.process_due(later())
    assert len(chatgpt["got"]) == n
    with session_scope() as db:
        assert "구독이 없습니다" in db.query(WakeJob).order_by(WakeJob.id.desc()).first().result


def test_subscribe_rejections(env, chatgpt):
    c, t = env
    bad = rpc(c, t["bob-ai"], "events/subscribe", {"name": "nope", "arguments": {},
                                                   "delivery": {"mode": "webhook", "url": CB, "secret": SECRET}})
    assert bad["error"]["code"] == -32602
    short = rpc(c, t["bob-ai"], "events/subscribe", {"name": "message.received", "arguments": {},
                                                     "delivery": {"mode": "webhook", "url": CB, "secret": "whsec_YWJj"}})
    assert short["error"]["code"] == -32602
    # 사람 토큰은 구독 불가
    assert subscribe(c, t["bob"])["error"]["code"] == -32602
    # 콜백이 challenge 를 못 돌려주면 -32015
    chatgpt["echo"] = False
    err = subscribe(c, t["bob-ai"])["error"]
    assert err["code"] == -32015 and err["data"]["reason"] == "challenge_failed"
    with session_scope() as db:
        assert db.query(EventSubscription).count() == 0
    assert rpc(c, t["bob-ai"], "events/poll")["error"]["code"] == -32601


def test_callback_url_must_be_public_https(monkeypatch):
    with pytest.raises(events.RpcError):
        events.check_callback_url("http://example.com/x")
    monkeypatch.setattr(events.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(events.RpcError):
        events.check_callback_url("https://evil.example/x")
    monkeypatch.setattr(events.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 443))])
    with pytest.raises(events.RpcError):
        events.check_callback_url("https://evil.example/x")
    monkeypatch.setattr(events.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("104.18.32.47", 443))])
    events.check_callback_url("https://hooks.chatgpt.com/x")


def test_delivery_retry_and_gone(env, chatgpt):
    c, t = env
    subscribe(c, t["bob-ai"])
    chatgpt["status"] = 503
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "q", "to": "bob"})
    wake.process_due(later())
    with session_scope() as db:
        j = db.query(WakeJob).one()
        assert j.status == "pending" and j.attempts == 1
    first_ts = chatgpt["got"][-1]["headers"]["webhook-id"]
    chatgpt["status"] = 410
    wake.process_due(later(120))
    assert chatgpt["got"][-1]["headers"]["webhook-id"] == first_ts  # 재시도도 같은 eventId
    with session_scope() as db:
        assert db.query(WakeJob).one().status == "failed"
        assert db.query(EventSubscription).one().active is False


def test_appointment_wakes_chatgpt(env, chatgpt):
    c, t = env
    subscribe(c, t["bob-ai"], name="appointment.started")
    start = datetime.now(UTC) + timedelta(minutes=5)
    with session_scope() as db:
        from app.auth import resolve_token
        p = resolve_token(db, t["alice-ai"])
        schedule.create_appointment(db, p, title="맞추기", starts_at=start.isoformat(), duration_minutes=30, invite=["bob"])
    schedule.process_appointments(start + timedelta(seconds=1))
    wake.process_due(start + timedelta(seconds=2))
    ev = [g for g in chatgpt["got"] if g["body"].get("name") == "appointment.started"]
    assert ev and ev[0]["body"]["data"]["title"] == "맞추기" and ev[0]["body"]["data"]["session_id"]
    with session_scope() as db:
        assert db.query(Appointment).one().status == "live"


def test_wake_test_sends_test_event(env, chatgpt):
    c, t = env
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "chatgpt_events"})
    r = c.post("/api/wake/test", headers=H(t["bob"]), json={}).json()
    assert r["ok"] is False and "구독" in r["error"]
    subscribe(c, t["bob-ai"])
    r = c.post("/api/wake/test", headers=H(t["bob"]), json={}).json()
    assert r["ok"] is True
    assert chatgpt["got"][-1]["body"]["data"]["reason"] == "test"
    # 팀원 목록에 'ChatGPT 이벤트'로 답한다고 보인다
    mem = {m["name"]: m for m in c.get("/api/members", headers=H(t["alice"])).json()}
    assert mem["bob"]["ai_responds"]["mode"] == "chatgpt_events"


def test_chatgpt_scheduled_task_mode(env):
    c, t = env
    r = c.put("/api/wake", headers=H(t["bob"]), json={"mode": "scheduled", "client": "chatgpt", "schedule": "tiers"})
    assert r.status_code == 422  # ChatGPT 예약 작업은 최소 매시간
    r = c.put("/api/wake", headers=H(t["bob"]), json={"mode": "scheduled", "client": "chatgpt", "schedule": "hourly"})
    assert r.status_code == 200 and r.json()["client"] == "chatgpt"
    mem = {m["name"]: m for m in c.get("/api/members", headers=H(t["alice"])).json()}
    assert "ChatGPT 기억" in mem["bob"]["ai_responds"]["expect"]
    assert c.put("/api/wake", headers=H(t["bob"]), json={"mode": "scheduled", "client": "copilot"}).status_code == 422


def test_chatgpt_oauth_cimd(env):
    c, t = env
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["client_id_metadata_document_supported"] is True and asm["registration_endpoint"]
    cb = "https://chatgpt.com/connector_platform_oauth_redirect"
    cid = "https://chatgpt.com/.well-known/oauth-client/abc.json"
    verifier, challenge = _pkce()
    q = {"response_type": "code", "client_id": cid, "redirect_uri": cb, "state": "s",
         "code_challenge": challenge, "code_challenge_method": "S256", "scope": "hub", "resource": "http://testserver/mcp"}
    page = c.get("/authorize", params=q)
    assert page.status_code == 200 and "<b>chatgpt.com</b>" in page.text
    r = c.post("/authorize", data={**q, "hub_token": t["bob-ai"]}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(cb + "?code=")
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                 "redirect_uri": cb, "client_id": cid})
    assert tok.status_code == 200 and tok.json()["access_token"] == t["bob-ai"]
    # ChatGPT 브라우저 Origin 이 붙어도 거절되지 않는다
    r = c.post("/mcp", headers={"Authorization": f"Bearer {t['bob-ai']}", "Origin": "https://chatgpt.com",
                                "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert r.status_code == 200


def test_settings_flag(env):
    assert get_settings().events_allow_private is False
