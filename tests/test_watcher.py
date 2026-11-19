import os

os.environ.setdefault("HUB_DATABASE_URL", "sqlite://")

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "watcher"))
import hub_watcher as hw  # noqa: E402

from app import services as svc  # noqa: E402
from app.db import Base, get_engine, init_engine, session_scope  # noqa: E402
from app.main import app  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_agent.py"))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def hub_server():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off"))  # REST만 사용 (MCP 세션 매니저는 다른 테스트가 사용)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


@pytest.fixture()
def team(hub_server):
    init_engine("sqlite://")
    Base.metadata.create_all(get_engine())
    from app.auth import Principal
    from app.models import Team

    with session_scope() as db:
        a = svc.create_team(db, "t", "alice", "", "alice-claude", "claude")
    with session_scope() as db:
        team_id = db.query(Team).one().id
        code = svc.create_invite(db, Principal(team_id, "t", 0, "x", True))["code"]
        b = svc.join_team(db, code, "bob", "Unity", "bob-gpt", "chatgpt")
    return hub_server, a, b


def _cfg(url, token, tmp_path, name, **kw):
    cfg = hw.Config(hub_url=url, token=token)
    cfg.command = [sys.executable, FAKE, "{prompt}"]
    cfg.state_file = tmp_path / f"{name}.json"
    cfg.cooldown_seconds = 0
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _me(hub):
    me = hub.get("/api/me")
    me["area"] = ""
    return me


def test_watcher_wakes_and_replies(team, tmp_path):
    url, a, b = team
    alice = hw.Hub(url, a["human_token"])
    bob_ai = hw.Hub(url, b["agent"]["agent_token"])
    import json
    import urllib.request

    def post(token, path, body):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    # 공지는 깨우지 않음, 질문은 깨움
    post(a["human_token"], "/api/messages", {"to": "bob", "kind": "notice", "body": "공지"})
    mid = post(a["human_token"], "/api/messages", {"to": "bob", "body": "좌표계?"})["id"]
    cfg = _cfg(url, b["agent"]["agent_token"], tmp_path, "bob")
    state = hw.State(cfg.state_file)
    assert hw.tick(bob_ai, cfg, _me(bob_ai), state) == 1
    th = alice.get(f"/api/messages/{mid}", mark_read="false")
    assert th["replies"][0]["body"] == "자동 답변입니다" and th["status"] == "answered"
    # 같은 글로 다시 깨우지 않음
    assert hw.tick(bob_ai, cfg, _me(bob_ai), hw.State(cfg.state_file)) == 0


def test_ping_pong_stops(team, tmp_path):
    url, a, b = team
    import json
    import urllib.request

    def post(token, path, body):
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    A, B = a["agent"]["agent_token"], b["agent"]["agent_token"]
    mid = post(A, "/api/messages", {"to": "bob", "body": "AI가 시작한 질문"})["id"]
    ha, hb = hw.Hub(url, A), hw.Hub(url, B)
    ca = _cfg(url, A, tmp_path, "a", max_ai_chain=3)
    cb = _cfg(url, B, tmp_path, "b", max_ai_chain=3)
    sa, sb = hw.State(ca.state_file), hw.State(cb.state_file)
    runs = 0
    for _ in range(6):
        runs += hw.tick(hb, cb, _me(hb), sb)
        runs += hw.tick(ha, ca, _me(ha), sa)
    th = ha.get(f"/api/messages/{mid}", mark_read="false")
    # 원글(AI) + 답글 2개(AI) = 3 → 멈춤
    assert len(th["replies"]) == 2 and runs == 2
    # 사람이 끼어들면 다시 깨어남
    post(a["human_token"], f"/api/messages/{mid}/replies", {"body": "사람이 추가 질문"})
    assert hw.tick(hb, cb, _me(hb), sb) == 1


def test_rate_limit_and_quiet_hours(team, tmp_path):
    url, a, b = team
    import json
    import urllib.request
    from datetime import datetime

    for i in range(3):
        req = urllib.request.Request(url + "/api/messages", data=json.dumps({"to": "bob", "body": f"q{i}"}).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {a['human_token']}",
                                                             "Content-Type": "application/json"})
        urllib.request.urlopen(req)
    hb = hw.Hub(url, b["agent"]["agent_token"])
    cb = _cfg(url, b["agent"]["agent_token"], tmp_path, "b", max_runs_per_hour=1, max_threads_per_run=1)
    sb = hw.State(cb.state_file)
    assert hw.tick(hb, cb, _me(hb), sb) == 1
    assert hw.tick(hb, cb, _me(hb), sb) == 0  # 시간당 1회 한도
    assert hw.in_quiet_hours("23:00-08:00", datetime(2026, 1, 1, 2, 0))
    assert not hw.in_quiet_hours("23:00-08:00", datetime(2026, 1, 1, 12, 0))


def test_rejects_human_token(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text('[hub]\nurl = "http://x"\ntoken = "hubh_abc"\n')
    os.environ.pop("HUB_AGENT_TOKEN", None)
    with pytest.raises(SystemExit):
        hw.load_config(p)
