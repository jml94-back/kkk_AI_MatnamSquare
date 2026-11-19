"""서버 관리자용 CLI (라즈파이에서 docker compose exec hub python -m app.cli ...).

  create-team <팀이름> <관리자이름> [--area 담당] [--agent AI이름 --llm claude]
  invite <팀이름> [--hours 72] [--uses 10] [--admin]      → 초대 코드 출력
  add-agent <팀이름> <팀원이름> <AI이름> [--llm chatgpt]   → AI 토큰 출력
  rotate-member <팀이름> <팀원이름>                         → 사람 토큰 재발급
  disable-member <팀이름> <팀원이름> | disable-agent <팀이름> <AI이름>
  list
토큰·초대 코드는 해시만 저장되므로 출력될 때 바로 전달하세요.
"""

import argparse
import json
import sys

from sqlalchemy import select

from . import services as svc
from .auth import TOKEN_PREFIX_HUMAN, Principal, hash_token, new_token
from .db import create_all, session_scope
from .models import Agent, Member, Team


def _team(db, name) -> Team:
    t = db.scalar(select(Team).where(Team.name == name))
    if not t:
        sys.exit(f"팀 '{name}' 없음")
    return t


def _member(db, team, name) -> Member:
    m = db.scalar(select(Member).where(Member.team_id == team.id, Member.name == name))
    if not m:
        sys.exit(f"팀원 '{name}' 없음")
    return m


def _admin_principal(team: Team) -> Principal:
    # CLI는 서버 관리자이므로 팀 관리자 권한으로 동작
    return Principal(team.id, team.name, 0, "(server-admin)", True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("create-team"); s.add_argument("team"); s.add_argument("admin"); s.add_argument("--area", default="")
    s.add_argument("--agent"); s.add_argument("--llm", default="other")
    s = sub.add_parser("invite"); s.add_argument("team"); s.add_argument("--hours", type=int, default=72)
    s.add_argument("--uses", type=int, default=10); s.add_argument("--admin", action="store_true")
    s = sub.add_parser("add-agent"); s.add_argument("team"); s.add_argument("member"); s.add_argument("name"); s.add_argument("--llm", default="other")
    for c in ("rotate-member", "disable-member", "disable-agent"):
        s = sub.add_parser(c); s.add_argument("team"); s.add_argument("name")
    sub.add_parser("list")
    a = ap.parse_args(argv)

    create_all()
    try:
        with session_scope() as db:
            if a.cmd == "create-team":
                print(json.dumps(svc.create_team(db, a.team, a.admin, a.area, a.agent, a.llm), ensure_ascii=False, indent=2))
            elif a.cmd == "invite":
                p = _admin_principal(_team(db, a.team))
                out = svc.create_invite(db, p, expires_in_hours=a.hours, max_uses=a.uses, grants_admin=a.admin)
                print(json.dumps(out, ensure_ascii=False, indent=2))
            elif a.cmd == "add-agent":
                m = _member(db, _team(db, a.team), a.member)
                print(json.dumps(svc._create_agent(db, m, a.name, a.llm), ensure_ascii=False, indent=2))
            elif a.cmd == "rotate-member":
                m = _member(db, _team(db, a.team), a.name)
                t = new_token(TOKEN_PREFIX_HUMAN); m.token_hash = hash_token(t); print(t)
            elif a.cmd == "disable-member":
                m = _member(db, _team(db, a.team), a.name); m.active = False; m.token_hash = None
                for ag in m.agents:
                    ag.active = False
                print("비활성화됨")
            elif a.cmd == "disable-agent":
                t = _team(db, a.team)
                ag = db.scalar(select(Agent).where(Agent.team_id == t.id, Agent.name == a.name))
                if not ag:
                    sys.exit("AI 없음")
                ag.active = False; print("비활성화됨")
            elif a.cmd == "list":
                for t in db.scalars(select(Team).order_by(Team.name)):
                    print(f"[{t.name}]")
                    for m in db.scalars(select(Member).where(Member.team_id == t.id).order_by(Member.name)):
                        flag = " (관리자)" if m.is_admin else ""
                        state = "" if m.active else " [비활성]"
                        print(f"  - {m.name}{flag}{state}  담당: {m.area}")
                        for ag in m.agents:
                            print(f"      · AI {ag.name} ({ag.llm}){'' if ag.active else ' [비활성]'}")
    except svc.HubError as e:
        sys.exit(f"오류: {e.detail}")


if __name__ == "__main__":
    main()
