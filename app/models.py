from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now(UTC)


class Team(Base):
    """팀. 모든 데이터(메시지·명세·결정)는 팀 단위로 분리된다."""

    __tablename__ = "team"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Invite(Base):
    """팀 초대 코드. 코드 원문은 저장하지 않고 해시만 저장."""

    __tablename__ = "invite"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(8))  # 코드 앞 4글자 (목록에서 구분용)
    created_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"), nullable=True)
    grants_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    max_uses: Mapped[int] = mapped_column(Integer, default=10)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Member(Base):
    """팀원(사람). 한 사람이 여러 팀에 들어가면 팀마다 별도 팀원/토큰을 가진다."""

    __tablename__ = "member"
    __table_args__ = (UniqueConstraint("team_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    area: Mapped[str] = mapped_column(String(200), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    team: Mapped[Team] = relationship()
    agents: Mapped[list["Agent"]] = relationship(back_populates="member")


class Agent(Base):
    """팀원의 AI. 토큰으로 인증하며 결정 승인 권한이 없다."""

    __tablename__ = "agent"
    __table_args__ = (UniqueConstraint("team_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    name: Mapped[str] = mapped_column(String(64))
    # 어떤 LLM인지 표시용 (claude / chatgpt / gemini / 기타 자유 문자열)
    llm: Mapped[str] = mapped_column(String(32), default="other")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    member: Mapped[Member] = relationship(back_populates="agents")


message_spec_ref = Table(
    "message_spec_ref",
    Base.metadata,
    Column("message_id", ForeignKey("message.id", ondelete="CASCADE"), primary_key=True),
    Column("spec_version_id", ForeignKey("spec_version.id", ondelete="CASCADE"), primary_key=True),
)


class Message(Base):
    """게시판 메시지. parent_id가 있으면 답글."""

    __tablename__ = "message"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"), nullable=True, index=True)
    sender_member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    sender_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    # None 이면 팀 전체
    to_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="question")  # question/request/notice/reply
    subject: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # 큰 파일은 업로드하지 않고 GitHub 등 외부 링크로만 참조한다
    links: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open/answered/resolved
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    sender_member: Mapped[Member] = relationship(foreign_keys=[sender_member_id])
    sender_agent: Mapped["Agent | None"] = relationship(foreign_keys=[sender_agent_id])
    to_member: Mapped["Member | None"] = relationship(foreign_keys=[to_member_id])
    spec_refs: Mapped[list["SpecVersion"]] = relationship(secondary=message_spec_ref)


class MessageRead(Base):
    """팀원별 읽음 표시 (사람과 그 사람의 AI가 공유)."""

    __tablename__ = "message_read"
    message_id: Mapped[int] = mapped_column(ForeignKey("message.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), primary_key=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Spec(Base):
    __tablename__ = "spec"
    __table_args__ = (UniqueConstraint("team_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    versions: Mapped[list["SpecVersion"]] = relationship(back_populates="spec", order_by="SpecVersion.version")


class SpecVersion(Base):
    __tablename__ = "spec_version"
    __table_args__ = (UniqueConstraint("spec_id", "version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    spec_id: Mapped[int] = mapped_column(ForeignKey("spec.id"))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(500), default="")
    links: Mapped[list] = mapped_column(JSON, default=list)
    author_member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decision.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    spec: Mapped[Spec] = relationship(back_populates="versions")
    author: Mapped[Member] = relationship()


class Decision(Base):
    """AI/사람이 제안, 사람만 승인/거절. 승인 시 proposed_content가 있으면 명세 새 버전으로 반영."""

    __tablename__ = "decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    rationale: Mapped[str] = mapped_column(Text, default="")
    spec_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposed_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    links: Mapped[list] = mapped_column(JSON, default=list)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)  # proposed/approved/rejected
    proposer_member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    proposer_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    decided_by_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    proposer_member: Mapped[Member] = relationship(foreign_keys=[proposer_member_id])
    proposer_agent: Mapped["Agent | None"] = relationship(foreign_keys=[proposer_agent_id])
    decided_by: Mapped["Member | None"] = relationship(foreign_keys=[decided_by_member_id])
