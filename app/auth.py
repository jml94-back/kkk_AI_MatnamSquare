import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Agent, Member

TOKEN_PREFIX_HUMAN = "hubh_"
TOKEN_PREFIX_AGENT = "huba_"


INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 헷갈리는 0/O/1/I 제외


def new_invite_code() -> str:
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_invite_code(code: str) -> str:
    raw = "".join(ch for ch in code.upper() if ch.isalnum())
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    team_id: int
    team_name: str
    member_id: int
    member_name: str
    is_admin: bool
    agent_id: int | None = None
    agent_name: str | None = None

    @property
    def is_human(self) -> bool:
        return self.agent_id is None

    @property
    def label(self) -> str:
        return f"{self.member_name}/{self.agent_name}" if self.agent_name else self.member_name


def resolve_token(db: Session, token: str | None) -> Principal | None:
    if not token:
        return None
    h = hash_token(token.strip())
    if token.startswith(TOKEN_PREFIX_AGENT):
        a = db.scalar(select(Agent).where(Agent.token_hash == h))
        if a and hmac.compare_digest(a.token_hash, h) and a.active and a.member.active:
            return Principal(a.team_id, a.member.team.name, a.member.id, a.member.name, a.member.is_admin, a.id, a.name)
        return None
    m = db.scalar(select(Member).where(Member.token_hash == h))
    if m and m.token_hash and hmac.compare_digest(m.token_hash, h) and m.active:
        return Principal(m.team_id, m.team.name, m.id, m.name, m.is_admin)
    return None


def extract_token(headers, query_params, allow_query: bool) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if headers.get("x-hub-token"):
        return headers.get("x-hub-token").strip()
    if allow_query and query_params.get("token"):
        return query_params.get("token")
    return None
