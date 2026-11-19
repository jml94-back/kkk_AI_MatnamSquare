"""REST API와 MCP 도구가 공유하는 비즈니스 로직."""

import difflib
import re
from datetime import UTC, datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from .auth import Principal
from .config import get_settings
from datetime import timedelta

from .auth import (TOKEN_PREFIX_AGENT, TOKEN_PREFIX_HUMAN, hash_token, new_invite_code, new_token,
                   normalize_invite_code)
from .models import Agent, Decision, Invite, Member, Message, MessageRead, Spec, SpecVersion, Team

MESSAGE_KINDS = {"question", "request", "notice"}
MESSAGE_STATUSES = {"open", "answered", "resolved"}
DECISION_STATUSES = {"proposed", "approved", "rejected"}


class HubError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _check_len(text: str, field: str):
    if len(text or "") > get_settings().max_body_chars:
        raise HubError(413, f"{field} 길이가 너무 깁니다 (최대 {get_settings().max_body_chars}자)")


MAX_LINKS = 20


def _clean_links(links: list | None) -> list[dict]:
    """[{"url": "https://...", "title": "..."}] 또는 ["https://..."] 를 정규화."""
    out = []
    for item in (links or [])[:MAX_LINKS]:
        if isinstance(item, str):
            item = {"url": item}
        url = str(item.get("url", "")).strip()
        if not re.match(r"^https?://[^\s]+$", url) or len(url) > 2000:
            raise HubError(422, f"링크는 http(s) URL이어야 합니다: {url[:100]}")
        out.append({"url": url, "title": str(item.get("title", ""))[:200]})
    return out


NAME_RE = re.compile(r"^[\w.\- ]{1,64}$")


def _check_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise HubError(422, f"{what} 이름은 1~64자의 글자/숫자/공백/._- 만 쓸 수 있습니다")
    return name


def _member_by_name(db: Session, team_id: int, name: str) -> Member:
    m = db.scalar(select(Member).where(Member.team_id == team_id, Member.name == name, Member.active.is_(True)))
    if not m:
        raise HubError(404, f"팀원 '{name}'을(를) 찾을 수 없습니다")
    return m


# ---------------------------------------------------------------- 팀원
def list_members(db: Session, p: Principal) -> list[dict]:
    rows = db.scalars(
        select(Member).where(Member.team_id == p.team_id, Member.active.is_(True)).order_by(Member.name)
    ).all()
    return [
        {
            "name": m.name,
            "area": m.area,
            "is_admin": m.is_admin,
            "agents": [{"name": a.name, "llm": a.llm} for a in m.agents if a.active],
        }
        for m in rows
    ]


def update_my_area(db: Session, p: Principal, area: str) -> dict:
    if not p.is_human:
        raise HubError(403, "사람 토큰만 프로필을 바꿀 수 있습니다")
    m = db.get(Member, p.member_id)
    m.area = area[:200]
    db.flush()
    return {"name": m.name, "area": m.area}


def remove_member(db: Session, p: Principal, name: str) -> dict:
    if not (p.is_human and p.is_admin):
        raise HubError(403, "팀 관리자(사람)만 팀원을 내보낼 수 있습니다")
    m = _member_by_name(db, p.team_id, name)
    m.active = False
    m.token_hash = None
    for a in m.agents:
        a.active = False
    db.flush()
    return {"removed": name}


# ---------------------------------------------------------------- 팀 / 초대 / AI 토큰
def create_team(db: Session, team_name: str, admin_name: str, area: str = "", agent_name: str | None = None, agent_llm: str = "other") -> dict:
    """새 팀과 첫 관리자를 만든다. 사람 토큰(+선택적으로 AI 토큰)을 돌려준다."""
    team_name = _check_name(team_name, "팀")
    admin_name = _check_name(admin_name, "팀원")
    if db.scalar(select(Team).where(Team.name == team_name)):
        raise HubError(409, "이미 있는 팀 이름입니다")
    team = Team(name=team_name)
    db.add(team)
    db.flush()
    return _add_member(db, team, admin_name, area, is_admin=True, agent_name=agent_name, agent_llm=agent_llm)


def _add_member(db: Session, team: Team, name: str, area: str, is_admin: bool, agent_name: str | None, agent_llm: str = "other") -> dict:
    if db.scalar(select(Member).where(Member.team_id == team.id, Member.name == name)):
        raise HubError(409, f"팀 '{team.name}'에 이미 '{name}' 이(가) 있습니다")
    t = new_token(TOKEN_PREFIX_HUMAN)
    m = Member(team_id=team.id, name=name, area=area[:200], is_admin=is_admin, token_hash=hash_token(t))
    db.add(m)
    db.flush()
    out = {"team": team.name, "member": name, "is_admin": is_admin, "human_token": t}
    if agent_name:
        out["agent"] = _create_agent(db, m, agent_name, agent_llm)
    return out


def _create_agent(db: Session, m: Member, name: str, llm: str = "other") -> dict:
    name = _check_name(name, "AI")
    llm = re.sub(r"[^\w.\-]", "", (llm or "other").lower())[:32] or "other"
    if db.scalar(select(Agent).where(Agent.team_id == m.team_id, Agent.name == name)):
        raise HubError(409, f"이미 있는 AI 이름입니다: {name}")
    t = new_token(TOKEN_PREFIX_AGENT)
    db.add(Agent(team_id=m.team_id, member_id=m.id, name=name, llm=llm, token_hash=hash_token(t)))
    db.flush()
    return {"name": name, "llm": llm, "agent_token": t}


def create_agent(db: Session, p: Principal, name: str, llm: str = "other") -> dict:
    if not p.is_human:
        raise HubError(403, "AI 토큰은 사람 토큰으로만 발급할 수 있습니다")
    return _create_agent(db, db.get(Member, p.member_id), name, llm)


def list_my_agents(db: Session, p: Principal) -> list[dict]:
    m = db.get(Member, p.member_id)
    return [{"name": a.name, "llm": a.llm, "active": a.active, "created_at": _iso(a.created_at)} for a in m.agents]


def revoke_agent(db: Session, p: Principal, name: str) -> dict:
    if not p.is_human:
        raise HubError(403, "사람 토큰만 AI 토큰을 폐기할 수 있습니다")
    a = db.scalar(select(Agent).where(Agent.team_id == p.team_id, Agent.name == name))
    if not a or (a.member_id != p.member_id and not p.is_admin):
        raise HubError(404, "내 AI 중에 그 이름이 없습니다")
    a.active = False
    db.flush()
    return {"revoked": name}


def create_invite(db: Session, p: Principal, *, expires_in_hours: int = 72, max_uses: int = 10, grants_admin: bool = False) -> dict:
    if not (p.is_human and p.is_admin):
        raise HubError(403, "팀 관리자(사람)만 초대 코드를 만들 수 있습니다")
    if not (1 <= expires_in_hours <= 24 * 30) or not (1 <= max_uses <= 100):
        raise HubError(422, "유효기간은 1시간~30일, 사용 횟수는 1~100 이어야 합니다")
    code = new_invite_code()
    inv = Invite(
        team_id=p.team_id, code_hash=hash_token(code), hint=code[:4], created_by_member_id=p.member_id or None,
        grants_admin=grants_admin, max_uses=max_uses,
        expires_at=datetime.now(UTC) + timedelta(hours=expires_in_hours),
    )
    db.add(inv)
    db.flush()
    return {"code": code, **invite_to_dict(inv)}


def invite_to_dict(i: Invite) -> dict:
    return {
        "id": i.id, "hint": i.hint + "-…", "max_uses": i.max_uses, "uses": i.uses,
        "grants_admin": i.grants_admin, "expires_at": _iso(i.expires_at), "revoked": i.revoked,
    }


def list_invites(db: Session, p: Principal) -> list[dict]:
    if not (p.is_human and p.is_admin):
        raise HubError(403, "팀 관리자만 볼 수 있습니다")
    rows = db.scalars(select(Invite).where(Invite.team_id == p.team_id).order_by(Invite.id.desc())).all()
    return [invite_to_dict(i) for i in rows]


def revoke_invite(db: Session, p: Principal, invite_id: int) -> dict:
    if not (p.is_human and p.is_admin):
        raise HubError(403, "팀 관리자만 폐기할 수 있습니다")
    i = db.get(Invite, invite_id)
    if not i or i.team_id != p.team_id:
        raise HubError(404, "초대 코드가 없습니다")
    i.revoked = True
    db.flush()
    return invite_to_dict(i)


def join_team(db: Session, code: str, name: str, area: str = "", agent_name: str | None = None, agent_llm: str = "other") -> dict:
    """초대 코드로 팀에 들어간다. 잘못된 코드는 모두 같은 오류를 돌려준다."""
    name = _check_name(name, "팀원")
    inv = db.scalar(
        select(Invite).where(Invite.code_hash == hash_token(normalize_invite_code(code))).with_for_update()
    )
    exp = inv.expires_at if inv else None
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if not inv or inv.revoked or inv.uses >= inv.max_uses or exp < datetime.now(UTC):
        raise HubError(403, "초대 코드가 올바르지 않거나 만료되었습니다")
    team = db.get(Team, inv.team_id)
    out = _add_member(db, team, name, area, is_admin=inv.grants_admin, agent_name=agent_name, agent_llm=agent_llm)
    inv.uses += 1
    db.flush()
    return out


# ---------------------------------------------------------------- 메시지
def message_to_dict(db: Session, m: Message, viewer: Principal | None = None, with_replies=False) -> dict:
    d = {
        "id": m.id,
        "parent_id": m.parent_id,
        "kind": m.kind,
        "subject": m.subject,
        "body": m.body,
        "tags": m.tags or [],
        "links": m.links or [],
        "status": m.status,
        "from": m.sender_member.name,
        "from_agent": m.sender_agent.name if m.sender_agent else None,
        "from_llm": m.sender_agent.llm if m.sender_agent else None,
        "written_by": "ai" if m.sender_agent_id else "human",
        "auto": m.auto_via,
        "to": m.to_member.name if m.to_member else None,
        "spec_refs": [{"spec": v.spec.name, "version": v.version} for v in m.spec_refs],
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
    }
    if viewer is not None:
        d["read"] = bool(
            db.get(MessageRead, {"message_id": m.id, "member_id": viewer.member_id})
        )
    if with_replies:
        replies = db.scalars(select(Message).where(Message.parent_id == m.id).order_by(Message.id)).all()
        d["replies"] = [message_to_dict(db, r, viewer) for r in replies]
    else:
        d["reply_count"] = db.scalar(select(func.count()).where(Message.parent_id == m.id))
    return d


def _resolve_spec_refs(db: Session, team_id: int, refs: list[dict] | None) -> list[SpecVersion]:
    out = []
    for r in (refs or [])[:20]:
        name, ver = r.get("spec"), r.get("version")
        spec = db.scalar(select(Spec).where(Spec.team_id == team_id, Spec.name == name))
        if not spec:
            raise HubError(404, f"명세 '{name}'이(가) 없습니다")
        q = select(SpecVersion).where(SpecVersion.spec_id == spec.id)
        q = q.where(SpecVersion.version == ver) if ver else q.order_by(SpecVersion.version.desc())
        sv = db.scalars(q).first()
        if not sv:
            raise HubError(404, f"명세 '{name}' v{ver}이(가) 없습니다")
        out.append(sv)
    return out


def create_message(
    db: Session,
    p: Principal,
    *,
    body: str,
    subject: str = "",
    to: str | None = None,
    kind: str = "question",
    tags: list[str] | None = None,
    spec_refs: list[dict] | None = None,
    links: list | None = None,
) -> dict:
    if kind not in MESSAGE_KINDS:
        raise HubError(422, f"kind는 {sorted(MESSAGE_KINDS)} 중 하나여야 합니다")
    if not body.strip():
        raise HubError(422, "본문이 비어 있습니다")
    _check_len(body, "본문")
    to_member = _member_by_name(db, p.team_id, to) if to else None
    m = Message(
        team_id=p.team_id,
        sender_member_id=p.member_id,
        sender_agent_id=p.agent_id,
        to_member_id=to_member.id if to_member else None,
        kind=kind,
        subject=subject[:200],
        body=body,
        tags=[t[:50] for t in (tags or [])][:20],
        links=_clean_links(links),
    )
    m.spec_refs = _resolve_spec_refs(db, p.team_id, spec_refs)
    from . import wake

    m.wake_depth = wake.depth_for_sender(db, p)
    db.add(m)
    db.flush()
    db.add(MessageRead(message_id=m.id, member_id=p.member_id))  # 내가 쓴 건 읽음 처리
    db.flush()
    wake.enqueue_for_message(db, m)
    db.refresh(m)
    return message_to_dict(db, m, p)


def list_messages(
    db: Session,
    p: Principal,
    *,
    to: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    unread_only: bool = False,
    include_broadcast: bool = True,
    limit: int = 50,
) -> list[dict]:
    q = select(Message).where(Message.team_id == p.team_id, Message.parent_id.is_(None))
    if to:
        target = _member_by_name(db, p.team_id, to)
        conds = [Message.to_member_id == target.id]
        if include_broadcast:
            conds.append(Message.to_member_id.is_(None))
        q = q.where(or_(*conds))
    if status:
        if status not in MESSAGE_STATUSES:
            raise HubError(422, f"status는 {sorted(MESSAGE_STATUSES)} 중 하나여야 합니다")
        q = q.where(Message.status == status)
    if unread_only:
        # 원글을 안 읽었거나, 안 읽은 답글이 달린 스레드
        unread_root = ~exists().where(
            and_(MessageRead.message_id == Message.id, MessageRead.member_id == p.member_id)
        )
        reply = Message.__table__.alias("r")
        unread_reply = exists().where(
            and_(
                reply.c.parent_id == Message.id,
                ~exists().where(
                    and_(MessageRead.message_id == reply.c.id, MessageRead.member_id == p.member_id)
                ),
            )
        )
        q = q.where(or_(unread_root, unread_reply))
    rows = db.scalars(q.order_by(Message.id.desc()).limit(min(max(limit, 1), 200))).all()
    out = [message_to_dict(db, m, p) for m in rows]
    if tag:
        out = [m for m in out if tag in m["tags"]]
    return out


def inbox(db: Session, p: Principal, *, include_read=False, limit=50) -> list[dict]:
    """나(팀원) 또는 팀 전체에게 온 스레드 중 열린 것 + 내 글에 새 답글이 달린 스레드."""
    items = list_messages(db, p, to=p.member_name, unread_only=not include_read, limit=limit)
    seen = {m["id"] for m in items}
    # 내가 쓴 글에 달린 안 읽은 답글
    mine = list_messages(db, p, unread_only=not include_read, limit=limit)
    for m in mine:
        if m["from"] == p.member_name and m["id"] not in seen:
            items.append(m)
    items = [m for m in items if not (m["from"] == p.member_name and m["reply_count"] == 0)]
    items.sort(key=lambda m: m["id"], reverse=True)
    return items[:limit]


def _team_message(db: Session, p: Principal, message_id: int) -> Message | None:
    m = db.get(Message, message_id)
    return m if m and m.team_id == p.team_id else None


def get_message(db: Session, p: Principal, message_id: int, mark_read=True) -> dict:
    m = _team_message(db, p, message_id)
    if not m:
        raise HubError(404, "메시지가 없습니다")
    root = m if m.parent_id is None else db.get(Message, m.parent_id)
    d = message_to_dict(db, root, p, with_replies=True)
    if mark_read:
        ids = [root.id] + [r["id"] for r in d["replies"]]
        for i in ids:
            if not db.get(MessageRead, {"message_id": i, "member_id": p.member_id}):
                db.add(MessageRead(message_id=i, member_id=p.member_id))
        db.flush()
    if root.to_member_id == p.member_id:
        from .notes import related_for

        # 나에게 온 질문이면 관련된 내 AI 메모를 함께 보여 준다
        d["my_related_notes"] = related_for(db, p, f"{root.subject} {root.body}")
    return d


def reply(db: Session, p: Principal, message_id: int, body: str, spec_refs=None, status: str | None = None, links=None,
          *, auto_via: str | None = None, wake_depth: int | None = None) -> dict:
    """답글. auto_via/wake_depth 는 허브 대리 응답기가 쓸 때만 (외부 API로는 받지 않음)."""
    parent = _team_message(db, p, message_id)
    if not parent:
        raise HubError(404, "메시지가 없습니다")
    if parent.parent_id is not None:
        parent = db.get(Message, parent.parent_id)  # 스레드는 1단계
    if not body.strip():
        raise HubError(422, "본문이 비어 있습니다")
    _check_len(body, "본문")
    r = Message(
        team_id=p.team_id,
        parent_id=parent.id,
        sender_member_id=p.member_id,
        sender_agent_id=p.agent_id,
        to_member_id=parent.sender_member_id,
        kind="reply",
        subject="",
        body=body,
        tags=[],
        links=_clean_links(links),
        status="open",
        auto_via=auto_via,
    )
    r.spec_refs = _resolve_spec_refs(db, p.team_id, spec_refs)
    from . import wake

    r.wake_depth = wake_depth if wake_depth is not None else wake.depth_for_sender(db, p)
    db.add(r)
    db.flush()
    db.add(MessageRead(message_id=r.id, member_id=p.member_id))
    if status:
        _set_status(parent, status)
    elif parent.status == "open" and parent.sender_member_id != p.member_id and not auto_via:
        # 자동 답글은 '답변됨'으로 바꾸지 않는다 (주인이 아직 확인하지 않았으므로)
        parent.status = "answered"
    parent.updated_at = datetime.now(UTC)
    db.flush()
    wake.enqueue_for_message(db, r)
    db.refresh(r)
    return message_to_dict(db, r, p)


def _set_status(m: Message, status: str):
    if status not in MESSAGE_STATUSES:
        raise HubError(422, f"status는 {sorted(MESSAGE_STATUSES)} 중 하나여야 합니다")
    m.status = status


def update_message_status(db: Session, p: Principal, message_id: int, status: str) -> dict:
    m = _team_message(db, p, message_id)
    if not m or m.parent_id is not None:
        raise HubError(404, "원글 메시지가 없습니다")
    _set_status(m, status)
    db.flush()
    return message_to_dict(db, m, p)


# ---------------------------------------------------------------- 명세
def _find_spec(db: Session, team_id: int, name: str) -> Spec | None:
    return db.scalar(select(Spec).where(Spec.team_id == team_id, Spec.name == name))


def _spec_or_404(db: Session, team_id: int, name: str) -> Spec:
    s = _find_spec(db, team_id, name)
    if not s:
        raise HubError(404, f"명세 '{name}'이(가) 없습니다")
    return s


def version_to_dict(v: SpecVersion, with_content=True) -> dict:
    d = {
        "spec": v.spec.name,
        "title": v.spec.title,
        "version": v.version,
        "summary": v.summary,
        "links": v.links or [],
        "author": v.author.name,
        "decision_id": v.decision_id,
        "created_at": _iso(v.created_at),
    }
    if with_content:
        d["content"] = v.content
    return d


def list_specs(db: Session, p: Principal) -> list[dict]:
    out = []
    for s in db.scalars(select(Spec).where(Spec.team_id == p.team_id).order_by(Spec.name)).all():
        latest = s.versions[-1] if s.versions else None
        out.append(
            {
                "name": s.name,
                "title": s.title,
                "latest_version": latest.version if latest else None,
                "updated_at": _iso(latest.created_at) if latest else None,
            }
        )
    return out


def get_spec(db: Session, p: Principal, name: str, version: int | None = None) -> dict:
    s = _spec_or_404(db, p.team_id, name)
    if not s.versions:
        raise HubError(404, "버전이 없습니다")
    if version is None:
        return version_to_dict(s.versions[-1])
    for v in s.versions:
        if v.version == version:
            return version_to_dict(v)
    raise HubError(404, f"'{name}' v{version}이(가) 없습니다")


def list_spec_versions(db: Session, p: Principal, name: str) -> list[dict]:
    s = _spec_or_404(db, p.team_id, name)
    return [version_to_dict(v, with_content=False) for v in s.versions]


def spec_diff(db: Session, p: Principal, name: str, from_version: int, to_version: int | None = None) -> dict:
    a = get_spec(db, p, name, from_version)
    b = get_spec(db, p, name, to_version)
    diff = difflib.unified_diff(
        a["content"].splitlines(),
        b["content"].splitlines(),
        fromfile=f"{name}@v{a['version']}",
        tofile=f"{name}@v{b['version']}",
        lineterm="",
    )
    return {"spec": name, "from_version": a["version"], "to_version": b["version"], "diff": "\n".join(diff)}


def _add_version(db: Session, team_id: int, name: str, content: str, summary: str, author_id: int, title: str | None, decision_id=None, links=None):
    s = _find_spec(db, team_id, name)
    if not s:
        s = Spec(team_id=team_id, name=name, title=title or name)
        db.add(s)
        db.flush()
    elif title:
        s.title = title
    next_v = (db.scalar(select(func.max(SpecVersion.version)).where(SpecVersion.spec_id == s.id)) or 0) + 1
    v = SpecVersion(
        spec_id=s.id, version=next_v, content=content, summary=summary[:500],
        author_member_id=author_id, decision_id=decision_id, links=links or [],
    )
    db.add(v)
    db.flush()
    db.refresh(s)
    db.refresh(v)
    return v


def create_spec_version(db: Session, p: Principal, name: str, content: str, summary: str = "", title: str | None = None, base_version: int | None = None, links=None) -> dict:
    if not p.is_human:
        raise HubError(403, "AI 토큰은 명세를 직접 수정할 수 없습니다. propose_decision 으로 변경을 제안하세요.")
    _check_len(content, "명세 내용")
    if not name or "/" in name or len(name) > 100:
        raise HubError(422, "명세 이름이 올바르지 않습니다")
    if base_version is not None:
        s = _find_spec(db, p.team_id, name)
        cur = s.versions[-1].version if s and s.versions else 0
        if cur != base_version:
            raise HubError(409, f"충돌: 현재 최신 버전은 v{cur} 입니다 (base_version={base_version})")
    return version_to_dict(_add_version(db, p.team_id, name, content, summary, p.member_id, title, links=_clean_links(links)))


# ---------------------------------------------------------------- 결정
def decision_to_dict(d: Decision) -> dict:
    return {
        "id": d.id,
        "title": d.title,
        "rationale": d.rationale,
        "spec_name": d.spec_name,
        "base_version": d.base_version,
        "proposed_content": d.proposed_content,
        "links": d.links or [],
        "message_id": d.message_id,
        "status": d.status,
        "proposed_by": d.proposer_member.name,
        "proposed_by_agent": d.proposer_agent.name if d.proposer_agent else None,
        "proposed_by_llm": d.proposer_agent.llm if d.proposer_agent else None,
        "decided_by": d.decided_by.name if d.decided_by else None,
        "decision_note": d.decision_note,
        "created_at": _iso(d.created_at),
        "decided_at": _iso(d.decided_at),
    }


def propose_decision(
    db: Session, p: Principal, *, title: str, rationale: str = "", spec_name: str | None = None,
    proposed_content: str | None = None, message_id: int | None = None, links: list | None = None,
) -> dict:
    if not title.strip():
        raise HubError(422, "제목이 비어 있습니다")
    _check_len(rationale, "근거")
    _check_len(proposed_content or "", "제안 내용")
    if proposed_content is not None and not spec_name:
        raise HubError(422, "proposed_content를 주려면 spec_name도 필요합니다")
    base = None
    if spec_name:
        s = _find_spec(db, p.team_id, spec_name)
        base = s.versions[-1].version if s and s.versions else 0
    if message_id is not None and not _team_message(db, p, message_id):
        raise HubError(404, "연결할 메시지가 없습니다")
    d = Decision(
        team_id=p.team_id,
        title=title[:200], rationale=rationale, spec_name=spec_name, base_version=base,
        proposed_content=proposed_content, message_id=message_id, links=_clean_links(links),
        proposer_member_id=p.member_id, proposer_agent_id=p.agent_id,
    )
    db.add(d)
    db.flush()
    db.refresh(d)
    return decision_to_dict(d)


def list_decisions(db: Session, p: Principal, status: str | None = None, limit: int = 50) -> list[dict]:
    q = select(Decision).where(Decision.team_id == p.team_id)
    if status:
        if status not in DECISION_STATUSES:
            raise HubError(422, f"status는 {sorted(DECISION_STATUSES)} 중 하나여야 합니다")
        q = q.where(Decision.status == status)
    return [decision_to_dict(d) for d in db.scalars(q.order_by(Decision.id.desc()).limit(min(limit, 200))).all()]


def _team_decision(db: Session, p: Principal, decision_id: int) -> Decision | None:
    d = db.get(Decision, decision_id)
    return d if d and d.team_id == p.team_id else None


def get_decision(db: Session, p: Principal, decision_id: int) -> dict:
    d = _team_decision(db, p, decision_id)
    if not d:
        raise HubError(404, "결정이 없습니다")
    return decision_to_dict(d)


def decide(db: Session, p: Principal, decision_id: int, status: str, note: str = "", force: bool = False) -> dict:
    if not p.is_human:
        raise HubError(403, "AI 토큰은 결정을 승인/거절할 수 없습니다. 사람만 가능합니다.")
    if status not in {"approved", "rejected"}:
        raise HubError(422, "status는 approved 또는 rejected 여야 합니다")
    d = _team_decision(db, p, decision_id)
    if not d:
        raise HubError(404, "결정이 없습니다")
    if d.status != "proposed":
        raise HubError(409, f"이미 {d.status} 상태입니다")
    result: dict = {}
    if status == "approved" and d.spec_name and d.proposed_content is not None:
        s = _find_spec(db, p.team_id, d.spec_name)
        cur = s.versions[-1].version if s and s.versions else 0
        if cur != (d.base_version or 0) and not force:
            raise HubError(
                409,
                f"제안 이후 명세가 v{d.base_version}→v{cur} 로 바뀌었습니다. 확인 후 force=true 로 승인하세요.",
            )
        v = _add_version(db, p.team_id, d.spec_name, d.proposed_content, f"결정 #{d.id}: {d.title}", p.member_id, None, d.id, links=d.links)
        result["applied_spec_version"] = v.version
    d.status = status
    d.decided_by_member_id = p.member_id
    d.decided_at = datetime.now(UTC)
    d.decision_note = note
    db.flush()
    db.refresh(d)
    return {**decision_to_dict(d), **result}
