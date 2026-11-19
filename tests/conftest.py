import os

os.environ["HUB_DATABASE_URL"] = "sqlite://"
os.environ["HUB_WAKE_WORKER"] = "false"  # 테스트에서는 process_due 를 직접 부른다
os.environ.setdefault("HUB_SECRET_KEY", "test-secret")

import pytest
from fastapi.testclient import TestClient

from app import services as svc
from app.db import Base, get_engine, init_engine, session_scope
from app.main import app


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="session")
def client():
    # MCP 세션 매니저는 프로세스당 한 번만 run 가능 → 클라이언트는 세션 단위로 공유
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def env(client):
    init_engine("sqlite://")
    Base.metadata.create_all(get_engine())
    toks = {}
    with session_scope() as db:
        a = svc.create_team(db, "warehouse", "alice", "알고리즘", "alice-claude", "claude")
    toks["alice"], toks["alice-ai"] = a["human_token"], a["agent"]["agent_token"]
    code = client.post("/api/invites", headers=H(toks["alice"]), json={}).json()["code"]
    b = client.post("/api/join", json={"code": code.lower().replace("-", ""), "your_name": "bob", "area": "Unity",
                                        "agent_name": "bob-gpt", "agent_llm": "ChatGPT"}).json()
    toks["bob"], toks["bob-ai"] = b["human_token"], b["agent"]["agent_token"]
    yield client, toks


