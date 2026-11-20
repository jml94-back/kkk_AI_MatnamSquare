"""실시간(동기) 세션.

두 팀원이 각자 AI와 대화하는 동안, AI끼리 같은 방에서 메시지를 주고받는다.
채팅 앱은 도구 호출이 오래 걸리면 끊기므로 '답 기다리기'는 롱폴링으로 최대 ~50초만 기다리고,
답이 없으면 no_reply_yet 을 돌려 AI가 다시 호출하게 한다.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, select

from .auth import Principal
from .db import session_scope
from .models import LiveMessage, LiveParticipant, LiveSession, Member, Message, MessageRead
from .services import HubError, _check_len, _clean_links, _iso, _member_by_name, _team_message

MAX_PARTICIPANTS = 30  # 사람·AI 합쳐서 (10명 + 각자 AI 여러 개 정도 여유)
ONLINE_SECONDS = 150  # 최근 이만큼 안에 wait/send 했으면 접속 중으로 표시
MAX_WAIT_SECONDS = 50  # Claude 등 채팅 앱의 MCP 도구 타임아웃(60초)보다 짧게. 더 긴 값은 잘라서 씀
DEFAULT_WAIT_SECONDS = 45
POLL_INTERVAL = 1.0
IDLE_CLOSE_HOURS = 12  # 이 시간 동안 조용하면 자동 종료
INVITE_FRESH = timedelta(hours=2)  # check_in 이 '초대받은 세션'으로 보여 주는 기간 (마지막 대화 기준)
INVITE_SUBJECT = "[실시간 세션 초대]"


def _now():
    return datetime.now(UTC)


def _aware(dt):
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _label(member_name: str, agent_name: str | None) -> str:
    return f"{member_name}/{agent_name}" if agent_name else member_name


def _session(db, p: Principal, session_id: int) -> LiveSession:
    s = db.get(LiveSession, session_id)
    if not s or s.team_id != p.team_id:
        raise HubError(404, "세션이 없습니다")
    if s.status == "open" and _aware(s.last_activity_at) < _now() - timedelta(hours=IDLE_CLOSE_HOURS):
        s.status, s.closed_at = "closed", _now()
        db.flush()
    if s.status == "open":
        a = _appointment(db, s)
        if a and _aware(a.ends_at) <= _now():
            # 약속 시간이 끝났으면 워커를 기다리지 않고 바로 닫는다
            s.status, s.closed_at = "closed", _now()
            if a.status == "live":
                a.status = "done"
            db.flush()
    return s


def _appointment(db, s: LiveSession):
    from .schedule import session_appointment

    return session_appointment(db, s.id)


def _appt_info(db, s: LiveSession) -> dict | None:
    a = _appointment(db, s)
    if a is None:
        return None
    left = max(0, int((_aware(a.ends_at) - _now()).total_seconds() // 60))
    return {"appointment_id": a.id, "title": a.title, "agenda": a.agenda, "ends_at": _iso(a.ends_at),
            "minutes_left": left if s.status == "open" else 0}


def _participant(db, p: Principal, s: LiveSession) -> LiveParticipant | None:
    return db.scalar(
        select(LiveParticipant).where(
            LiveParticipant.session_id == s.id,
            LiveParticipant.member_id == p.member_id,
            LiveParticipant.agent_key == (p.agent_id or 0),
        )
    )


# 프로세스 메모리의 "세션별 마지막 메시지 id". 기다리는 참가자들이 DB를 매초 두드리지 않도록
# 이 값이 바뀌었을 때만 DB를 확인한다(워커 1개 기준. 여러 워커여도 주기적 DB 확인으로 동작은 함).
_latest: dict[int, int] = {}
DB_RECHECK_SECONDS = 10


def _bump(session_id: int, message_id: int):
    if _latest.get(session_id, 0) < message_id:
        _latest[session_id] = message_id


def _is_to(m: LiveMessage, p: Principal) -> bool:
    if not m.to_names:
        return False
    me = {p.member_name, _label(p.member_name, p.agent_name)}
    return any(n in me for n in m.to_names)


def _msg(m: LiveMessage, viewer: Principal | None = None) -> dict:
    d = {
        "id": m.id,
        "from": _label(m.sender_member.name, m.sender_agent.name if m.sender_agent else None),
        "from_member": m.sender_member.name,
        "written_by": "ai" if m.sender_agent_id else "human",
        "llm": m.sender_agent.llm if m.sender_agent else None,
        "body": m.body,
        "links": m.links or [],
        "to": m.to_names or [],
        "created_at": _iso(m.created_at),
    }
    if viewer is not None:
        d["to_me"] = _is_to(m, viewer)
    return d


def session_to_dict(db, s: LiveSession, p: Principal | None = None) -> dict:
    parts = db.scalars(select(LiveParticipant).where(LiveParticipant.session_id == s.id)).all()
    d = {
        "session_id": s.id,
        "topic": s.topic,
        "status": s.status,
        "invited": s.invited or [],
        "notified": s.notified or [],
        "created_by": s.created_by.name,
        "message_id": s.message_id,
        "participants": [
            {"name": _label(x.member.name, x.agent.name if x.agent else None),
             "kind": "ai" if x.agent_id else "human",
             "llm": x.agent.llm if x.agent else None,
             "online": _aware(x.last_seen_at) > _now() - timedelta(seconds=ONLINE_SECONDS),
             "last_seen_at": _iso(x.last_seen_at)}
            for x in parts
        ],
        "created_at": _iso(s.created_at),
        "last_activity_at": _iso(s.last_activity_at),
    }
    appt = _appt_info(db, s)
    if appt:
        d["appointment"] = appt
    if p is not None:
        d["joined"] = any(x.member_id == p.member_id and x.agent_key == (p.agent_id or 0) for x in parts)
        d["invited_me"] = p.member_name in (s.invited or [])
    return d


def _join(db, p: Principal, s: LiveSession) -> LiveParticipant:
    part = _participant(db, p, s)
    if part:
        return part
    if s.invited and p.member_name not in s.invited and s.created_by_member_id != p.member_id:
        raise HubError(403, "이 세션에 초대되지 않았습니다")
    count = len(db.scalars(select(LiveParticipant.id).where(LiveParticipant.session_id == s.id)).all())
    if count >= MAX_PARTICIPANTS:
        raise HubError(409, f"세션 정원({MAX_PARTICIPANTS})이 찼습니다")
    # 새 참가자는 지금까지의 대화를 history 로 받고, 이후 메시지부터 기다린다
    part = LiveParticipant(session_id=s.id, member_id=p.member_id, agent_id=p.agent_id, agent_key=p.agent_id or 0)
    db.add(part)
    db.flush()
    return part


def _history(db, s: LiveSession, limit=50, viewer: Principal | None = None) -> list[dict]:
    rows = db.scalars(
        select(LiveMessage).where(LiveMessage.session_id == s.id).order_by(LiveMessage.id.desc()).limit(limit)
    ).all()
    return [_msg(m, viewer) for m in reversed(rows)]


# ------------------------------------------------------------------ 동기 함수 (DB)
def create_session(db, p: Principal, topic: str = "", invite: list[str] | None = None,
                   message_id: int | None = None) -> dict:
    names = []
    for n in invite or []:
        names.append(_member_by_name(db, p.team_id, n).name)
    if message_id is not None and not _team_message(db, p, message_id):
        raise HubError(404, "연결할 메시지가 없습니다")
    s = LiveSession(team_id=p.team_id, topic=topic[:200], invited=names, created_by_member_id=p.member_id,
                    created_by_agent_id=p.agent_id, message_id=message_id)
    db.add(s)
    db.flush()
    _join(db, p, s)
    woken = notify_invited(db, p, s, [_member_by_name(db, p.team_id, n) for n in names])
    return {**session_to_dict(db, s, p), "invite_wake": woken, "next_step": _next_step(woken)}


# ------------------------------------------------------------------ 초대 알림 + 깨우기
def _next_step(woken: list[dict]) -> str:
    if not woken:
        return ("invite 를 비워 두면 아무도 깨우지 않습니다. 특정 팀원의 AI를 부르려면 invite_live_session 으로 초대하세요. "
                "wait_live 로 다른 참가자를 기다리면 됩니다.")
    later = [w["name"] for w in woken if not w["auto"]]
    msg = ("초대한 팀원의 AI에게 허브가 직접 알렸습니다. 사람에게 '세션에 참가하라고 전해 달라'고 할 필요 없습니다. "
           "wait_live 로 상대 AI가 들어와 말하기를 기다리세요.")
    if later:
        msg += (f" 단, {', '.join(later)}님은 허브가 바로 깨울 수 없어서(woken_how 참고) 다음 확인 때나 "
                "주인이 AI를 열 때 들어옵니다.")
    return msg


def _wake_how(db, member_id: int) -> tuple[bool, str]:
    """(허브가 바로 깨울 수 있나, 사람에게 보여 줄 설명)"""
    from .models import WakeConfig
    from .wake import CLIENT_APPS, SCHEDULE_LABELS

    c = db.scalar(select(WakeConfig).where(WakeConfig.member_id == member_id))
    if not c or not c.enabled:
        return False, "깨우기 미설정: 주인이 AI를 열면 check_in/read_inbox 에서 초대를 봅니다"
    if c.mode == "claude_routine":
        return True, "Claude 루틴을 바로 깨움 (몇 분 안)"
    if c.mode == "chatgpt_events":
        return True, "ChatGPT 이벤트로 바로 깨움 (몇 분 안)"
    if c.mode == "scheduled":
        app = CLIENT_APPS.get(c.client or "claude", "Claude")
        return False, f"{app} 예약 확인 때 들어옴 ({SCHEDULE_LABELS.get(c.model or 'hourly', '')})"
    return False, "대리 응답 방식은 실시간 세션에 들어오지 않습니다"


def notify_invited(db, p: Principal, s: LiveSession, members: list[Member]) -> list[dict]:
    """초대받은 팀원에게 초대 글을 보내고, 깨우기를 설정했으면 그 AI를 바로 깨운다.
    세션을 연 사람, 지금 부르는 사람, 이미 알린 사람, 이미 들어온 사람은 건너뛴다."""
    from .wake import enqueue_live_invite

    done = set(s.notified or [])
    joined = set(db.scalars(select(LiveParticipant.member_id).where(LiveParticipant.session_id == s.id)))
    me = _label(p.member_name, p.agent_name)
    out = []
    for m in {x.id: x for x in members}.values():
        if m.id in (s.created_by_member_id, p.member_id) or m.name in done or m.id in joined:
            continue
        others = [n for n in (s.invited or []) if n != m.name]
        body = (f"{me}님이 실시간 세션 #{s.id} 에 {m.name}님의 AI를 불렀습니다 (사람이 아니라 AI끼리의 대화입니다).\n\n"
                f"- 주제: {s.topic or '(없음)'}\n- 연 사람: {s.created_by.name}\n"
                + (f"- 함께 초대된 팀원: {', '.join(others)}\n" if others else "")
                + f"\nAI는 join_live_session(session_id={s.id}) 로 들어가 지금까지의 대화를 읽고, "
                  "wait_live / send_live 를 반복하며 대화하세요. 나를 지목한(to_me) 말에 답하고, 세션이 closed 가 되면 끝냅니다. "
                  "결정 승인·사람의 일정·비용 약속은 하지 말고 주인에게 남겨 두세요.")
        note = Message(team_id=s.team_id, sender_member_id=p.member_id, sender_agent_id=p.agent_id, to_member_id=m.id,
                       kind="request", subject=f"{INVITE_SUBJECT} {s.topic or f'세션 #{s.id}'}"[:200], body=body,
                       tags=["live-session", f"live-{s.id}"], links=[])
        db.add(note)
        db.flush()
        db.add(MessageRead(message_id=note.id, member_id=p.member_id))
        auto, how = _wake_how(db, m.id)
        queued = enqueue_live_invite(db, p, s, m, note)
        if auto and not queued:
            auto, how = False, "이번에는 바로 깨우지 못함 (AI끼리 연쇄 한도 등). 주인이 AI를 열면 초대를 봅니다"
        done.add(m.name)
        out.append({"name": m.name, "auto": auto, "woken_how": how})
    s.notified = sorted(done)
    db.flush()
    return out


def invite(db, p: Principal, session_id: int, members: list[str]) -> dict:
    """열린 세션에 팀원을 더 부른다 (그 팀원의 AI를 깨움)."""
    s = _session(db, p, session_id)
    if s.status != "open":
        raise HubError(409, "이미 종료된 세션입니다")
    if s.invited and p.member_name not in s.invited and s.created_by_member_id != p.member_id:
        raise HubError(403, "이 세션에 초대되지 않았습니다")
    found = [_member_by_name(db, p.team_id, n.split("/", 1)[0].strip()) for n in members or [] if n.strip()]
    if not found:
        raise HubError(422, "members 에 부를 팀원 이름을 한 명 이상 적으세요")
    if s.invited:
        s.invited = list(dict.fromkeys([*s.invited, *(m.name for m in found)]))
    woken = notify_invited(db, p, s, found)
    return {**session_to_dict(db, s, p), "invite_wake": woken, "next_step": _next_step(woken)}


def pending_invites(db, p: Principal) -> list[dict]:
    """나를 부른(초대 알림이 온) 열린 세션 중 이 AI가 아직 들어가지 않은 것 (check_in 용)."""
    out = []
    rows = db.scalars(select(LiveSession).where(LiveSession.team_id == p.team_id, LiveSession.status == "open")
                      .order_by(LiveSession.id)).all()
    for s in rows:
        if p.member_name not in (s.notified or []):
            continue
        s = _session(db, p, s.id)
        if s.status != "open" or _aware(s.last_activity_at) < _now() - INVITE_FRESH or _participant(db, p, s):
            continue
        out.append({"session_id": s.id, "topic": s.topic, "created_by": s.created_by.name,
                    "invited": s.invited or [], "created_at": _iso(s.created_at)})
    return out


def _mark_invites_read(db, p: Principal, s: LiveSession):
    """세션에 들어오면 그 세션의 초대 글은 읽은 것으로 (check_in 이 같은 일을 계속 시키지 않게)."""
    notes = db.scalars(select(Message).where(Message.team_id == s.team_id, Message.to_member_id == p.member_id,
                                             Message.subject.startswith(INVITE_SUBJECT))).all()
    for n in notes:
        if f"live-{s.id}" in (n.tags or []) and not db.get(MessageRead, (n.id, p.member_id)):
            db.add(MessageRead(message_id=n.id, member_id=p.member_id))


def list_sessions(db, p: Principal, include_closed: bool = False) -> list[dict]:
    q = select(LiveSession).where(LiveSession.team_id == p.team_id)
    if not include_closed:
        q = q.where(LiveSession.status == "open")
    out = []
    for s in db.scalars(q.order_by(LiveSession.id.desc()).limit(30)).all():
        s = _session(db, p, s.id)  # 오래된 세션 자동 종료 반영
        if include_closed or s.status == "open":
            out.append(session_to_dict(db, s, p))
    return out


def join_session(db, p: Principal, session_id: int) -> dict:
    s = _session(db, p, session_id)
    if s.status != "open":
        raise HubError(409, "이미 종료된 세션입니다")
    part = _join(db, p, s)
    _mark_invites_read(db, p, s)
    history = _history(db, s, viewer=p)
    if history:
        part.last_seen_id = max(part.last_seen_id, history[-1]["id"])
    part.last_seen_at = _now()
    return {**session_to_dict(db, s, p), "history": history}


def _check_to(db, p: Principal, s: LiveSession, to: list[str] | None) -> tuple[list[str], list[Member]]:
    """지목 대상 확인. 아직 안 들어온 팀원을 지목하면 그 팀원도 초대하고 AI를 깨운다 (돌려주는 두 번째 값)."""
    if not to:
        return [], []
    parts = db.scalars(select(LiveParticipant).where(LiveParticipant.session_id == s.id)).all()
    valid = set()
    for x in parts:
        valid.add(x.member.name)
        if x.agent:
            valid.add(_label(x.member.name, x.agent.name))
    bad, call = [], {}
    for n in to:
        if n in valid:
            continue
        m = db.scalar(select(Member).where(Member.team_id == s.team_id, Member.name == n.split("/", 1)[0],
                                           Member.active.is_(True)))
        if m is None:
            bad.append(n)
        else:
            call[m.id] = m
    if bad:
        raise HubError(422, f"팀원이 아닙니다: {', '.join(bad[:5])}. 이름은 list_members 로 확인하세요")
    if call and s.invited:
        s.invited = list(dict.fromkeys([*s.invited, *(m.name for m in call.values())]))
    return list(dict.fromkeys(to))[:MAX_PARTICIPANTS], list(call.values())


def send(db, p: Principal, session_id: int, body: str, links=None, to: list[str] | None = None) -> dict:
    s = _session(db, p, session_id)
    if s.status != "open":
        raise HubError(409, "이미 종료된 세션입니다")
    if not body.strip():
        raise HubError(422, "본문이 비어 있습니다")
    _check_len(body, "본문")
    part = _join(db, p, s)
    to_names, call = _check_to(db, p, s, to)
    m = LiveMessage(session_id=s.id, sender_member_id=p.member_id, sender_agent_id=p.agent_id, body=body,
                    links=_clean_links(links), to_names=to_names)
    db.add(m)
    db.flush()
    woken = notify_invited(db, p, s, call) if call else []
    s.last_activity_at = _now()
    part.last_seen_at = _now()
    db.refresh(m)
    # 커밋된 뒤에 기다리는 쪽을 깨운다 (커밋 전에 깨우면 아직 안 보일 수 있음)
    sid, mid = s.id, m.id
    event.listen(db, "after_commit", lambda _s: _bump(sid, mid), once=True)
    out = _msg(m, p)
    if woken:
        out["invite_wake"] = woken  # 아직 안 들어온 팀원을 지목해서 그 AI를 불렀음
    return out


def _poll_once(p: Principal, session_id: int, mentions_only: bool = False) -> dict | None:
    """다른 참가자의 새 메시지가 있으면 돌려주고 커서를 옮긴다. 없으면 None.
    mentions_only 면 나를 지목한 메시지나 사람이 직접 쓴 메시지가 올 때만 깨어나고,
    깨어날 때는 그 사이의 모든 새 메시지를 함께 돌려준다(맥락 유지)."""
    with session_scope() as db:
        s = _session(db, p, session_id)
        part = _participant(db, p, s)
        if part is None:
            raise HubError(409, "먼저 세션에 참가하세요 (join)")
        part.last_seen_at = _now()
        rows = db.scalars(
            select(LiveMessage)
            .where(LiveMessage.session_id == s.id, LiveMessage.id > part.last_seen_id)
            .order_by(LiveMessage.id)
        ).all()
        if rows:
            _bump(s.id, rows[-1].id)
        new = [m for m in rows if not (m.sender_member_id == p.member_id and (m.sender_agent_id or 0) == (p.agent_id or 0))]
        if s.status != "open":
            if rows:
                part.last_seen_id = rows[-1].id
            return {"status": "closed", "session_status": s.status, "messages": [_msg(m, p) for m in new]}
        if mentions_only and not any(_is_to(m, p) or m.sender_agent_id is None for m in new):
            return None  # 커서를 옮기지 않아 나중에 함께 받는다
        if rows:
            part.last_seen_id = rows[-1].id
        if new:
            out = {"status": "new_messages", "session_status": s.status, "messages": [_msg(m, p) for m in new]}
            appt = _appt_info(db, s)
            if appt:
                out["minutes_left"] = appt["minutes_left"]
            asked = " ".join(m.body for m in new if _is_to(m, p))
            if asked:
                from .notes import related_for

                # 나를 지목한 질문과 관련된 내 메모를 함께 돌려준다 (답할 때 참고)
                out["my_related_notes"] = related_for(db, p, asked)
            return out
        return None


async def wait(p: Principal, session_id: int, timeout: float = DEFAULT_WAIT_SECONDS,
               mentions_only: bool = False) -> dict:
    """다른 참가자의 메시지가 올 때까지 최대 timeout 초 기다린다 (롱폴링)."""
    timeout = max(0.0, min(float(timeout), MAX_WAIT_SECONDS))
    deadline = time.monotonic() + timeout
    seen = None
    last_db = 0.0
    while True:
        cur = _latest.get(session_id)
        if seen is None or cur != seen or time.monotonic() - last_db >= DB_RECHECK_SECONDS:
            seen, last_db = cur, time.monotonic()
            res = await asyncio.to_thread(_poll_once, p, session_id, mentions_only)
            if res is not None:
                return res
        if time.monotonic() >= deadline:
            return {
                "status": "no_reply_yet",
                "session_status": "open",
                "messages": [],
                "hint": "아직 답이 없습니다. 사용자에게 계속 기다릴지 물어보거나, 바로 다시 wait 를 호출하세요.",
            }
        await asyncio.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.monotonic())) or 0.05)


async def send_and_wait(p: Principal, session_id: int, body: str, links=None,
                        timeout: float = DEFAULT_WAIT_SECONDS, to: list[str] | None = None,
                        mentions_only: bool = False) -> dict:
    def _send():
        with session_scope() as db:
            return send(db, p, session_id, body, links, to)

    sent = await asyncio.to_thread(_send)
    return {"sent": sent, **(await wait(p, session_id, timeout, mentions_only))}


def close_session(db, p: Principal, session_id: int, summary: str | None = None) -> dict:
    """세션 종료. summary 를 주면 합의 내용을 게시판에 공지로 남겨 나중에 다른 사람도 볼 수 있게 한다."""
    s = _session(db, p, session_id)
    if s.status == "open":
        s.status, s.closed_at = "closed", _now()
        db.flush()
    out = session_to_dict(db, s, p)
    if summary and summary.strip():
        from .services import create_message, reply

        body = f"실시간 세션 #{s.id} 요약\n\n{summary}"
        if s.message_id:
            out["summary_message"] = reply(db, p, s.message_id, body)
        else:
            out["summary_message"] = create_message(
                db, p, body=body, subject=f"[세션 요약] {s.topic}"[:200], kind="notice", tags=["live-session"]
            )
    return out


def transcript(db, p: Principal, session_id: int, limit: int = 200) -> dict:
    s = _session(db, p, session_id)
    return {**session_to_dict(db, s, p), "messages": _history(db, s, min(limit, 500), viewer=p)}
