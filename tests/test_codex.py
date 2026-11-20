"""Codex(CLI·IDE) 연결 안내와 PC 예약 실행(codex exec) 깨우기."""
import html
import re

from conftest import H

from app import schedule


def _codex_agent(c, t):
    r = c.post("/api/agents", headers=H(t["bob"]), json={"name": "bob-codex", "llm": "codex"})
    assert r.status_code == 201
    return r.json()["agent"]["agent_token"]


def test_codex_scheduled_mode(env):
    c, t = env
    _codex_agent(c, t)
    # Codex 는 PC 작업 스케줄러가 돌리므로 활동 시간대별(3/5/15분)도 된다
    r = c.put("/api/wake", headers=H(t["bob"]), json={"mode": "scheduled", "client": "codex", "schedule": "tiers",
                                                      "agent": "bob-codex"})
    assert r.status_code == 200, r.text
    assert r.json()["client"] == "codex" and r.json()["schedule"] == "tiers"
    mem = {m["name"]: m for m in c.get("/api/members", headers=H(t["alice"])).json()}
    expect = mem["bob"]["ai_responds"]["expect"]
    assert "Codex" in expect and "허브 메모" in expect


def test_check_schedule_gives_codex_prompt(env):
    c, t = env
    tok = _codex_agent(c, t)
    j = c.get("/api/check-schedule", headers=H(tok)).json()
    assert j["task_prompt"] == schedule.CODEX_TASK_PROMPT
    assert "/codex/matnam-wake" in j["how_to"] and j["check_plan"]
    # Claude AI 에게는 원래 지시문
    assert c.get("/api/check-schedule", headers=H(t["alice-ai"])).json()["task_prompt"] == schedule.TASK_PROMPT
    # Codex 지시문은 채팅 기억 대신 허브 메모를 쓰고 8단계를 지킨다
    p = schedule.CODEX_TASK_PROMPT
    for word in ("check_in", "search_notes", "read_note", "join_live_session", "read_inbox", "create_appointment", "save_my_note"):
        assert word in p
    assert re.findall(r"^(\d)\. ", p, re.M) == [str(i) for i in range(1, 9)]


def test_codex_wake_scripts(env):
    c, _ = env
    sh = c.get("/codex/matnam-wake.sh")
    assert sh.status_code == 200
    body = sh.text
    assert "__HUB_URL__" not in body and "__PROMPT__" not in body
    assert schedule.CODEX_TASK_PROMPT in body
    assert '"$HUB/api/check-in"' in body and "codex exec --skip-git-repo-check" in body
    ps = c.get("/codex/matnam-wake.ps1")
    raw = ps.content
    assert raw.startswith(b"\xef\xbb\xbf")  # Windows PowerShell 5.1 이 한글을 읽도록 BOM
    text = raw.decode("utf-8-sig")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert schedule.CODEX_TASK_PROMPT.replace("\n", "\r\n") in text
    assert c.get("/codex/../main.py").status_code == 404
    assert c.get("/codex/other.sh").status_code == 404


def test_join_page_codex(env):
    c, _ = env
    page = c.get("/join").text
    shown = html.unescape(re.search(r'<code id="w-prompt-codex"[^>]*>(.*?)</code>', page, re.S).group(1))
    assert shown == schedule.CODEX_TASK_PROMPT
    assert 'value="scheduled_codex"' in page and "<option>codex</option>" in page
    assert "bearer_token_env_var" in page and "codex mcp add" in page
