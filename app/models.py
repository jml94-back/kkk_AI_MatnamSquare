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
    # 깨우기 연쇄 깊이 (사람·평소 AI = 0, 깨워진 AI가 쓴 글 = 깨운 글 + 1). AI끼리 끝없이 깨우는 것을 막는다
    wake_depth: Mapped[int] = mapped_column(Integer, default=0)
    # 자동으로 쓴 글이면 그 방식 (responder / routine). 사람이 직접 시킨 글이면 None
    auto_via: Mapped[str | None] = mapped_column(String(16), nullable=True)
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


class LiveSession(Base):
    """실시간(동기) 대화 방. 두 사람이 각자 AI와 대화하는 동안 AI끼리 직접 주고받는다."""

    __tablename__ = "live_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    topic: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open/closed
    invited: Mapped[list] = mapped_column(JSON, default=list)  # 초대한 팀원 이름들 (비어 있으면 팀 누구나)
    created_by_member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("message.id"), nullable=True)  # 관련 게시판 스레드
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[Member] = relationship(foreign_keys=[created_by_member_id])


class LiveParticipant(Base):
    """세션 참가자. 사람 토큰과 각 AI 토큰이 각각 참가자. last_seen_id 는 어디까지 읽었는지."""

    __tablename__ = "live_participant"
    __table_args__ = (UniqueConstraint("session_id", "member_id", "agent_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("live_session.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    agent_key: Mapped[int] = mapped_column(Integer, default=0)  # agent_id or 0 (UNIQUE 에 NULL 을 피하려고)
    last_seen_id: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    member: Mapped[Member] = relationship()
    agent: Mapped["Agent | None"] = relationship()


class LiveMessage(Base):
    __tablename__ = "live_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("live_session.id", ondelete="CASCADE"), index=True)
    sender_member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    sender_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    links: Mapped[list] = mapped_column(JSON, default=list)
    # 지목한 참가자 ("팀원" 또는 "팀원/AI"). 비어 있으면 방 전체에게. 지목해도 모두가 볼 수 있다.
    to_names: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    sender_member: Mapped[Member] = relationship(foreign_keys=[sender_member_id])
    sender_agent: Mapped["Agent | None"] = relationship(foreign_keys=[sender_agent_id])


class AgentNote(Base):
    """AI별 메모(마크다운 지식 노트). 그 AI의 담당 영역·핵심 사실을 남겨 두면,
    누가 그 AI에게 물을 때 관련 메모가 함께 떠오르고, 그 AI가 없을 때도 팀원이 찾아볼 수 있다."""

    __tablename__ = "agent_note"
    __table_args__ = (UniqueConstraint("agent_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    links: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by_member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    updated_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    agent: Mapped[Agent] = relationship(foreign_keys=[agent_id])


class WakeConfig(Base):
    """팀원별 'AI 깨우기' 설정. 그 팀원에게 메시지가 오면 허브가 그 사람의 AI를 깨운다.
    - claude_routine: Claude Code 루틴의 API 트리거를 호출 → 클라우드 세션이 허브 커넥터로 답함
    - responder: 허브가 팀원의 API 키로 LLM을 직접 불러 AI 메모를 근거로 답함 (벤더 무관)
    비밀값(루틴 토큰·API 키)은 HUB_SECRET_KEY 로 암호화해 저장하고 응답에 절대 돌려주지 않는다."""

    __tablename__ = "wake_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), unique=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent.id"), index=True)
    mode: Mapped[str] = mapped_column(String(20))  # claude_routine / responder / scheduled / chatgpt_events
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    on_direct: Mapped[bool] = mapped_column(Boolean, default=True)  # 나에게 온 새 글
    on_replies: Mapped[bool] = mapped_column(Boolean, default=True)  # 내가 참여한 스레드의 새 답글
    on_broadcast: Mapped[bool] = mapped_column(Boolean, default=False)  # 팀 전체 공지
    routine_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)  # anthropic / openai / gemini
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)  # scheduled 방식에서는 주기(hourly 등)
    instructions: Mapped[str] = mapped_column(Text, default="")
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_per_hour: Mapped[int] = mapped_column(Integer, default=10)
    # 루틴 세션이 깨어 있는 동안 그 AI가 쓴 글은 active_depth 를 물려받는다
    active_depth: Mapped[int] = mapped_column(Integer, default=0)
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 이 사람의 AI가 마지막으로 inbox 를 확인한 시각 (scheduled 방식이 실제로 도는지 확인용)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # scheduled: 어느 앱의 예약 작업이 확인하나 (claude / chatgpt)
    client: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # scheduled: 답하는 Claude 프로젝트 이름 (표시용)
    project_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    member: Mapped[Member] = relationship()
    agent: Mapped[Agent] = relationship()


class WakeJob(Base):
    """깨우기 작업 큐. 메시지를 쓰는 같은 트랜잭션에서 쌓이고, 백그라운드 워커가 처리한다."""

    __tablename__ = "wake_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    config_id: Mapped[int] = mapped_column(ForeignKey("wake_config.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("message.id", ondelete="CASCADE"))
    root_id: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(16))  # direct / reply / broadcast / appointment
    appointment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # reason=appointment 일 때
    depth: Mapped[int] = mapped_column(Integer, default=0)
    # pending → running → fired / skipped / failed / merged(다른 작업과 한 번에 처리됨)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class ActiveHours(Base):
    """팀원이 AI를 주로 쓰는 시간대 (3단계). 이 시간대에 따라 AI가 허브를 얼마나 자주 확인할지,
    허브가 얼마나 빨리 깨울지가 정해진다. busy/often 에 없는 시간은 모두 rare(거의 안 씀)."""

    __tablename__ = "active_hours"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Seoul")
    busy: Mapped[list] = mapped_column(JSON, default=list)  # 예: ["월-금 09-12", "월-금 14-18"]
    often: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    member: Mapped[Member] = relationship()


class Appointment(Base):
    """AI 약속. 정한 시각이 되면 허브가 실시간 세션을 열고 참가자들의 AI를 깨우며,
    끝나는 시각에 세션을 닫는다."""

    __tablename__ = "appointment"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    agenda: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # scheduled → live → done / cancelled / missed(서버가 꺼져 있어 시간이 지나 버림)
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    created_by_member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    created_by_agent_id: Mapped[int | None] = mapped_column(ForeignKey("agent.id"), nullable=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("live_session.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    created_by: Mapped[Member] = relationship(foreign_keys=[created_by_member_id])
    participants: Mapped[list["AppointmentParticipant"]] = relationship(
        back_populates="appointment", cascade="all, delete-orphan", order_by="AppointmentParticipant.id")


class AppointmentParticipant(Base):
    __tablename__ = "appointment_participant"
    __table_args__ = (UniqueConstraint("appointment_id", "member_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointment.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"))
    response: Mapped[str] = mapped_column(String(16), default="invited")  # invited / accepted / declined

    appointment: Mapped[Appointment] = relationship(back_populates="participants")
    member: Mapped[Member] = relationship()


class EventSubscription(Base):
    """ChatGPT 등 MCP 클라이언트의 이벤트 구독 (MCP Events, webhook 전달).

    클라이언트가 events/subscribe 로 '이 AI 앞으로 새 글이 오면 이 URL로 알려 달라'고 등록한다.
    구독 정체성 = (AI, 콜백 URL, 이벤트 이름, 인자) 이며 같은 정체성으로 다시 구독하면 갱신(만료 연장)된다.
    서명 비밀값(whsec_…)은 HUB_SECRET_KEY 로 암호화해 저장한다."""

    __tablename__ = "event_subscription"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # sub_… (정체성에서 결정적으로 만든 값)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id"), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agent.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    url: Mapped[str] = mapped_column(String(1000))
    secret_enc: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    client_name: Mapped[str] = mapped_column(String(100), default="")
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    agent: Mapped[Agent] = relationship()
