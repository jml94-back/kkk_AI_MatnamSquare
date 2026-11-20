from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import schedule, wake
from app.db import session_scope
from app.models import ActiveHours, WakeJob
from app.services import HubError

ROUTINE = "https://api.anthropic.com/v1/claude_code/routines/trig_01ABCdef/fire"
KST = ZoneInfo("Asia/Seoul")


def _cron_list(field, size):
    if field == "*":
        return list(range(size))
    out = []
    for part in field.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture()
def fired(monkeypatch):
    calls = []

    def http_post(url, *, headers, json, timeout=30):
        calls.append(json["text"])
        return httpx.Response(200, json={"claude_code_session_url": "https://claude.ai/code/session_x"})

    monkeypatch.setattr(wake, "http_post", http_post)
    return calls


def test_parse_range():
    r = schedule.parse_range("월-금 9시~12시")
    assert r["days"] == [0, 1, 2, 3, 4] and (r["start"], r["end"]) == (9, 12) and r["text"] == "월-금 09-12"
    assert schedule.parse_range("weekend 10:00-22:00")["days"] == [5, 6]
    assert schedule.parse_range("20-24")["text"] == "매일 20-24"
    assert schedule.parse_range("토,일 10-12")["days"] == [5, 6]
    assert schedule.parse_range("금-월 1-2")["days"] == [0, 4, 5, 6]
    for bad in ["월-금 22-02", "아무때나", "월-금 09-25", "xyz 9-10"]:
        with pytest.raises(HubError):
            schedule.parse_range(bad)


def test_tiers_and_plan():
    ah = ActiveHours(timezone="Asia/Seoul", busy=["월-금 09-12"], often=["월-금 08-18", "주말 10-22"])
    mon = lambda h: datetime(2026, 11, 23, h, 30, tzinfo=KST)  # 2026-11-23 은 월요일
    assert schedule.tier_at(ah, mon(10)) == "busy"
    assert schedule.tier_at(ah, mon(8)) == "often" and schedule.tier_at(ah, mon(13)) == "often"
    assert schedule.tier_at(ah, mon(20)) == "rare"
    assert schedule.tier_at(ah, datetime(2026, 11, 22, 11, tzinfo=KST)) == "often"  # 일요일
    assert schedule.tier_at(None) == "rare"
    plan = schedule.check_plan(ah)
    crons = {p["cron"] for p in plan}
    assert "*/3 9-11 * * 1-5" in crons
    assert "*/5 8,12-17 * * 1-5" in crons and "*/5 10-21 * * 0,6" in crons
    assert "*/15 0-7,18-23 * * 1-5" in crons and "*/15 0-9,22-23 * * 0,6" in crons
    # 같은 시각에 두 작업이 겹치지 않는다
    grid = {}
    for p in plan:
        _, hours, _, _, days = p["cron"].split()
        for d in _cron_list(days, 7):
            for h in _cron_list(hours, 24):
                assert (d, h) not in grid
                grid[(d, h)] = p["tier"]
    assert len(grid) == 7 * 24
    assert schedule.check_plan(None) == [{"tier": "rare", "label": "거의 안 씀", "every_minutes": 15,
                                          "cron": "*/15 * * * *", "text": "매일 하루 종일: 15분마다"}]


def test_active_hours_api(env):
    c, t = env
    r = c.put("/api/active-hours", headers=H(t["bob-ai"]), json={"busy": ["평일 9-12"], "often": ["매일 20-23"]})
    assert r.status_code == 200, r.text
    assert r.json()["busy"] == ["월-금 09-12"] and r.json()["timezone"] == "Asia/Seoul"
    assert c.put("/api/active-hours", headers=H(t["bob"]), json={"busy": ["언젠가"]}).status_code == 422
    assert c.put("/api/active-hours", headers=H(t["bob"]), json={"timezone": "Mars/Base"}).status_code == 422
    seen = c.get("/api/active-hours?member=bob", headers=H(t["alice"])).json()
    assert seen["configured"] and seen["check_plan"]
    members = {m["name"]: m for m in c.get("/api/members", headers=H(t["alice"])).json()}
    assert members["bob"]["active_now"]["tier"] in schedule.TIERS and members["alice"]["active_now"] is None


def test_team_union(env):
    """A가 3~6시, B가 4~9시면 팀 전체는 3~9시. 시간대가 달라도 UTC 로 맞춰 합친다."""
    c, t = env
    c.put("/api/active-hours", headers=H(t["alice"]), json={"busy": ["매일 03-06"]})
    r = c.put("/api/active-hours", headers=H(t["bob"]), json={"busy": ["매일 04-09"], "often": ["매일 20-22"]}).json()
    assert r["team"]["busy"] == ["매일 3시~9시"] and r["team"]["often"] == ["매일 20시~22시"]
    assert "*/3 3-8 * * *" in {x["cron"] for x in r["check_plan"]}
    # 개인 시간대는 그대로
    assert c.get("/api/active-hours", headers=H(t["alice"])).json()["busy"] == ["매일 03-06"]
    # bob 이 도쿄(+9, 서울과 같음)가 아니라 UTC 면 서울 기준 13~18시로 들어간다
    c.put("/api/active-hours", headers=H(t["bob"]), json={"busy": ["매일 04-09"], "timezone": "UTC"})
    team = c.get("/api/active-hours", headers=H(t["alice"])).json()["team"]
    assert team["busy"] == ["매일 3시~6시, 13시~18시"]
    # AI가 바로 예약 작업을 만들 수 있는 형태
    sch = c.get("/api/check-schedule", headers=H(t["alice-ai"])).json()
    assert sch["check_plan"] == team["check_plan"] and "check_in" in sch["task_prompt"]
    assert "save_my_note" in sch["task_prompt"] and "cron:" in sch["how_to"]


def test_wake_delay_follows_tier(env, fired, monkeypatch):
    c, t = env
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "claude_routine", "routine_url": ROUTINE,
                                                   "routine_token": "sk-ant-oat01-secret"})
    c.put("/api/active-hours", headers=H(t["bob"]), json={"busy": [], "often": []})  # 하루 종일 '거의 안 씀'
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "질문", "to": "bob"})
    now = datetime.now(UTC)
    wake.process_due(now + timedelta(minutes=1))
    assert fired == []  # 15분 동안 모은다
    wake.process_due(now + timedelta(minutes=16))
    assert len(fired) == 1
    # 가장 활발한 시간이면 바로
    c.put("/api/active-hours", headers=H(t["bob"]), json={"busy": ["매일 0-24"]})
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "또 질문", "to": "bob"})
    wake.process_due(now + timedelta(minutes=17, seconds=10))
    assert len(fired) == 2


def test_check_in(env):
    c, t = env
    r = c.get("/api/check-in", headers=H(t["bob-ai"])).json()
    assert r["has_work"] is False and r["unread_threads"] == 0 and "바로 끝내" in r["todo"][0]
    c.post("/api/messages", headers=H(t["alice"]), json={"body": "질문", "to": "bob"})
    r = c.get("/api/check-in", headers=H(t["bob-ai"])).json()
    assert r["has_work"] and r["unread_threads"] == 1


def test_appointment_flow(env, fired):
    c, t = env
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "claude_routine", "routine_url": ROUTINE,
                                                   "routine_token": "sk-ant-oat01-secret"})
    c.put("/api/wake", headers=H(t["alice"]), json={"mode": "scheduled", "schedule": "tiers"})
    start = datetime.now(KST) + timedelta(minutes=10)
    body = {"title": "좌표계 맞추기", "starts_at": start.strftime("%Y-%m-%d %H:%M"), "duration_minutes": 30,
            "invite": ["bob"], "agenda": "Y-up 인지 확인"}
    assert c.post("/api/appointments", headers=H(t["alice-ai"]), json={**body, "invite": []}).status_code == 422
    assert c.post("/api/appointments", headers=H(t["alice-ai"]),
                  json={**body, "starts_at": "2020-01-01 10:00"}).status_code == 422
    r = c.post("/api/appointments", headers=H(t["alice-ai"]), json=body)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["status"] == "scheduled" and a["duration_minutes"] == 30
    assert {x["name"]: x["response"] for x in a["participants"]} == {"alice": "accepted", "bob": "invited"}
    # 초대 글이 bob 에게 가지만, 초대만으로는 깨우지 않는다
    inbox = c.get("/api/inbox", headers=H(t["bob"])).json()
    assert any("[약속 초대]" in m["subject"] for m in inbox)
    wake.process_due(datetime.now(UTC) + timedelta(minutes=1))
    assert fired == []
    assert c.post(f"/api/appointments/{a['id']}/respond", headers=H(t["bob"]), json={"accept": True}).json()["my_response"] == "accepted"
    # 곧 시작할 약속은 check_in 에 보인다
    assert c.get("/api/check-in", headers=H(t["alice-ai"])).json()["appointments_soon"][0]["id"] == a["id"]

    # 약속 시각 → 세션 열림, bob 의 루틴 깨움 (시간당 한도와 상관없이)
    t0 = datetime.now(UTC) + timedelta(minutes=10, seconds=5)
    assert schedule.process_appointments(t0) == 1
    a = c.get("/api/appointments", headers=H(t["bob"])).json()[0]
    assert a["status"] == "live" and a["session_id"]
    sid = a["session_id"]
    wake.process_due(t0 + timedelta(seconds=1))
    assert len(fired) == 1 and f"세션 #{sid}" in fired[0] and "join_live_session" in fired[0]
    # Cowork(예약 확인) 쪽은 check_in 으로 알게 된다
    ci = c.get("/api/check-in", headers=H(t["alice-ai"])).json()
    assert ci["has_work"] and ci["appointments_now"][0]["session_id"] == sid and "join_live_session" in ci["todo"][0]

    # 세션 안에서 자유롭게 대화
    j = c.post(f"/api/live/{sid}/join", headers=H(t["bob-ai"])).json()
    assert j["appointment"]["title"] == "좌표계 맞추기" and j["appointment"]["agenda"] == "Y-up 인지 확인"
    c.post(f"/api/live/{sid}/join", headers=H(t["alice-ai"]))
    c.post(f"/api/live/{sid}/messages", headers=H(t["bob-ai"]), json={"body": "Y-up 맞나요?"})
    w = c.get(f"/api/live/{sid}/wait?timeout=1", headers=H(t["alice-ai"])).json()
    assert w["status"] == "new_messages" and "minutes_left" in w
    with session_scope() as db:
        assert db.query(WakeJob).filter(WakeJob.reason == "appointment").count() == 1

    # 끝나는 시각 → 세션 닫힘
    assert schedule.process_appointments(t0 + timedelta(minutes=31)) == 1
    s = c.get(f"/api/live/{sid}", headers=H(t["alice"])).json()
    assert s["status"] == "closed"
    assert c.post(f"/api/live/{sid}/messages", headers=H(t["bob-ai"]), json={"body": "더 할 말"}).status_code == 409
    assert c.get("/api/appointments?include_past=true", headers=H(t["alice"])).json()[0]["status"] == "done"
    # 닫힌 뒤에도 요약은 남길 수 있다
    r = c.post(f"/api/live/{sid}/close", headers=H(t["alice-ai"]), json={"summary": "Y-up 으로 합의"})
    assert r.status_code == 200 and r.json()["summary_message"]


def test_declined_and_cancel(env, fired):
    c, t = env
    c.put("/api/wake", headers=H(t["bob"]), json={"mode": "claude_routine", "routine_url": ROUTINE,
                                                   "routine_token": "sk-ant-oat01-secret"})
    start = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    a = c.post("/api/appointments", headers=H(t["alice"]), json={"title": "회의", "starts_at": start, "invite": ["bob"]}).json()
    c.post(f"/api/appointments/{a['id']}/respond", headers=H(t["bob"]), json={"accept": False})
    schedule.process_appointments(datetime.now(UTC) + timedelta(minutes=6))
    wake.process_due(datetime.now(UTC) + timedelta(minutes=7))
    assert fired == []  # 거절한 사람은 부르지 않음
    live = c.get("/api/appointments", headers=H(t["alice"])).json()[0]
    assert c.post(f"/api/live/{live['session_id']}/join", headers=H(t["bob-ai"])).status_code == 403
    assert c.post(f"/api/appointments/{a['id']}/cancel", headers=H(t["bob"])).status_code == 403
    r = c.post(f"/api/appointments/{a['id']}/cancel", headers=H(t["alice"]))
    assert r.json()["status"] == "cancelled"
    assert c.get(f"/api/live/{live['session_id']}", headers=H(t["alice"])).json()["status"] == "closed"


def test_missed_when_server_was_down(env):
    c, t = env
    start = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    c.post("/api/appointments", headers=H(t["alice"]), json={"title": "x", "starts_at": start, "duration_minutes": 5,
                                                              "invite": ["bob"]})
    schedule.process_appointments(datetime.now(UTC) + timedelta(hours=1))
    assert c.get("/api/appointments?include_past=true", headers=H(t["alice"])).json()[0]["status"] == "missed"


def test_mcp_tools_listed(env):
    from app.mcp_server import mcp
    import asyncio

    names = {x.name for x in asyncio.run(mcp.list_tools())}
    assert {"check_in", "get_check_schedule", "set_active_hours", "get_active_hours", "create_appointment", "list_appointments",
            "respond_appointment", "cancel_appointment"} <= names
