from conftest import H


def test_dashboard_page_served_with_security_headers(env):
    client, _ = env
    r = client.get("/dashboard")
    assert r.status_code == 200 and "허브 대시보드" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # 다른 AI가 쓴 본문은 textContent 로만 넣는다
    assert "innerHTML" not in r.text
    # 대시보드 전용 API는 AI용 OpenAPI 에 나오지 않는다
    assert "/api/dashboard/week" not in client.get("/openapi.json").json()["paths"]


def test_dashboard_week_grid(env):
    client, t = env
    assert client.get("/api/dashboard/week").status_code == 401
    client.put("/api/active-hours", headers=H(t["alice"]), json={"busy": ["월-금 09-12"], "often": ["매일 20-23"]})
    client.put("/api/active-hours", headers=H(t["bob-ai"]), json={"busy": ["월 12-14"]})
    w = client.get("/api/dashboard/week", headers=H(t["bob"])).json()
    assert w["timezone"] == "Asia/Seoul" and 0 <= w["now"]["weekday"] <= 6 and 0 <= w["now"]["hour"] <= 23
    rows = {m["member"]: m for m in w["members"]}
    assert set(rows) == {"alice", "bob"}
    a = rows["alice"]["grid"]
    assert len(a) == 7 and all(len(d) == 24 for d in a)
    assert a[0][9] == "busy" and a[0][12] == "rare" and a[6][21] == "often" and a[5][10] == "rare"
    team = w["team"]["grid"]
    # 팀 시간표는 합집합: 월요일 9~14시는 alice 와 bob 이 이어서 '가장 활발'
    assert [team[0][h] for h in range(8, 15)] == ["rare", "busy", "busy", "busy", "busy", "busy", "rare"]
    assert team[0][21] == "often"


def test_dashboard_week_unset_member(env):
    client, t = env
    w = client.get("/api/dashboard/week", headers=H(t["alice"])).json()
    for m in w["members"]:
        assert m["configured"] is False and all(c == "rare" for d in m["grid"] for c in d)
