"""AI별 메모 (agent memo).

각 AI가 자기 담당 영역의 핵심 지식을 마크다운 메모로 허브에 남긴다.
- 쓰기: 그 AI 자신, 또는 그 AI의 주인(사람 토큰)
- 읽기·검색: 같은 팀 모두
- 누군가 그 AI(또는 그 AI의 주인)를 지목해 물으면, 질문과 관련된 메모를 찾아 함께 보여 준다.
채팅 앱 안의 AI 메모리는 허브가 직접 읽을 수 없으므로, 공유하고 싶은 지식은 여기에 옮겨 둔다.
"""

import re
from datetime import UTC, datetime

from sqlalchemy import select

from .auth import Principal
from .models import Agent, AgentNote, Member
from .services import HubError, _check_len, _clean_links, _iso

KEY_RE = re.compile(r"^[\w.\-]{1,100}$")
MAX_NOTES_PER_AGENT = 200
WORD_RE = re.compile(r"[\w\-]{2,}")
STOPWORDS = {"the", "and", "for", "with", "this", "that", "있나요", "인가요", "어떻게", "무엇", "뭐야", "알려줘", "해주세요"}


def _tokens(text: str) -> set[str]:
    out = set()
    for w in WORD_RE.findall((text or "").lower()):
        if w in STOPWORDS:
            continue
        out.add(w)
        # 한국어 조사 대충 떼기 (좌표계는 → 좌표계)
        if len(w) > 2 and re.match(r"[가-힣]", w[-1]):
            out.add(w[:-1])
    return out


def _agent_by_label(db, p: Principal, name: str) -> Agent:
    """'AI이름' 또는 '팀원/AI이름'."""
    agent_name = name.split("/", 1)[-1]
    a = db.scalar(select(Agent).where(Agent.team_id == p.team_id, Agent.name == agent_name, Agent.active.is_(True)))
    if not a:
        raise HubError(404, f"AI '{name}'을(를) 찾을 수 없습니다")
    return a


def _target_agent(db, p: Principal, agent: str | None) -> Agent:
    """쓰기 대상 AI. AI 토큰은 자기 자신만, 사람 토큰은 자기 AI 중 하나를 지정."""
    if not p.is_human:
        if agent and agent.split("/", 1)[-1] != p.agent_name:
            raise HubError(403, "AI는 자기 메모만 쓸 수 있습니다")
        return db.get(Agent, p.agent_id)
    if not agent:
        raise HubError(422, "사람 토큰으로 쓸 때는 어느 AI의 메모인지 agent 를 지정하세요")
    a = _agent_by_label(db, p, agent)
    if a.member_id != p.member_id:
        raise HubError(403, "내 AI의 메모만 쓸 수 있습니다")
    return a


def note_to_dict(n: AgentNote, with_content=True) -> dict:
    d = {
        "agent": f"{n.agent.member.name}/{n.agent.name}",
        "llm": n.agent.llm,
        "key": n.key,
        "title": n.title,
        "tags": n.tags or [],
        "links": n.links or [],
        "version": n.version,
        "updated_at": _iso(n.updated_at),
    }
    if with_content:
        d["content"] = n.content
    return d


def save_note(db, p: Principal, key: str, content: str, title: str = "", tags: list[str] | None = None,
              links=None, agent: str | None = None) -> dict:
    a = _target_agent(db, p, agent)
    if not KEY_RE.match(key or ""):
        raise HubError(422, "key 는 1~100자의 글자/숫자/._- 만 쓸 수 있습니다 (예: coordinate-system)")
    if not (content or "").strip():
        raise HubError(422, "내용이 비어 있습니다")
    _check_len(content, "메모")
    n = db.scalar(select(AgentNote).where(AgentNote.agent_id == a.id, AgentNote.key == key))
    if n is None:
        count = len(db.scalars(select(AgentNote.id).where(AgentNote.agent_id == a.id)).all())
        if count >= MAX_NOTES_PER_AGENT:
            raise HubError(409, f"메모는 AI당 {MAX_NOTES_PER_AGENT}개까지입니다")
        n = AgentNote(team_id=p.team_id, agent_id=a.id, key=key, content=content, version=1,
                      updated_by_member_id=p.member_id, updated_by_agent_id=p.agent_id)
        db.add(n)
    else:
        n.content = content
        n.version += 1
        n.updated_by_member_id, n.updated_by_agent_id = p.member_id, p.agent_id
        n.updated_at = datetime.now(UTC)
    n.title = (title or n.title or key)[:200]
    if tags is not None:
        n.tags = [t[:50] for t in tags][:20]
    if links is not None:
        n.links = _clean_links(links)
    db.flush()
    db.refresh(n)
    return note_to_dict(n)


def delete_note(db, p: Principal, key: str, agent: str | None = None) -> dict:
    a = _target_agent(db, p, agent)
    n = db.scalar(select(AgentNote).where(AgentNote.agent_id == a.id, AgentNote.key == key))
    if not n:
        raise HubError(404, "메모가 없습니다")
    db.delete(n)
    db.flush()
    return {"deleted": key}


def get_note(db, p: Principal, agent: str, key: str) -> dict:
    a = _agent_by_label(db, p, agent)
    n = db.scalar(select(AgentNote).where(AgentNote.agent_id == a.id, AgentNote.key == key))
    if not n:
        raise HubError(404, "메모가 없습니다")
    return note_to_dict(n)


def _agents_of(db, p: Principal, owner: str | None) -> list[int] | None:
    """owner: 'AI이름', '팀원/AI이름', 또는 '팀원'(그 사람의 모든 AI). None 이면 팀 전체."""
    if not owner:
        return None
    if "/" in owner:
        return [_agent_by_label(db, p, owner).id]
    m = db.scalar(select(Member).where(Member.team_id == p.team_id, Member.name == owner))
    if m:
        return [a.id for a in m.agents if a.active]
    return [_agent_by_label(db, p, owner).id]


def search_notes(db, p: Principal, query: str = "", owner: str | None = None, limit: int = 10,
                 with_content: bool = False) -> list[dict]:
    q = select(AgentNote).where(AgentNote.team_id == p.team_id)
    ids = _agents_of(db, p, owner)
    if ids is not None:
        q = q.where(AgentNote.agent_id.in_(ids or [-1]))
    notes = [n for n in db.scalars(q).all() if n.agent.active]
    toks = _tokens(query)
    scored = []
    for n in notes:
        if toks:
            title, tags, body = _tokens(n.title + " " + n.key), _tokens(" ".join(n.tags or [])), n.content.lower()
            score = sum(3 * (t in title) + 3 * (t in tags) + min(body.count(t), 5) for t in toks)
            if score == 0:
                continue
        else:
            score = 0
        scored.append((score, n.updated_at, n))
    scored.sort(key=lambda x: (x[0], _iso(x[1])), reverse=True)
    out = []
    for score, _, n in scored[: max(1, min(limit, 50))]:
        d = note_to_dict(n, with_content=with_content)
        if not with_content:
            d["snippet"] = _snippet(n.content, toks)
        if toks:
            d["score"] = score
        out.append(d)
    return out


def _snippet(content: str, toks: set[str], width: int = 200) -> str:
    low = content.lower()
    pos = min((low.find(t) for t in toks if low.find(t) >= 0), default=0)
    start = max(0, pos - 60)
    s = content[start:start + width].replace("\n", " ")
    return ("…" if start else "") + s + ("…" if start + width < len(content) else "")


def related_for(db, p: Principal, text: str, limit: int = 3) -> list[dict]:
    """나(p)에게 온 질문 text 와 관련된 '내' 메모. AI면 자기 메모, 사람이면 자기 AI들의 메모."""
    owner = f"{p.member_name}/{p.agent_name}" if p.agent_name else p.member_name
    try:
        hits = search_notes(db, p, text, owner=owner, limit=limit)
    except HubError:
        return []
    return [{"key": h["key"], "title": h["title"], "snippet": h["snippet"], "agent": h["agent"]} for h in hits]
