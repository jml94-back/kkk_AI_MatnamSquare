from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HUB_", env_file=".env", extra="ignore")

    # postgresql+psycopg://user:pass@host:5432/db  (테스트에서는 sqlite 사용)
    database_url: str = "postgresql+psycopg://hub:hub@db:5432/hub"
    # MCP 엔드포인트에 허용할 Host 헤더(쉼표 구분). 비우면 Host 검사 안 함.
    public_hosts: str = ""
    # MCP 커넥터가 헤더를 못 붙이는 경우를 위해 ?token= 쿼리 허용 여부
    allow_query_token: bool = True
    # 메시지·명세는 텍스트(마크다운)만. 큰 파일은 links 로 GitHub 등을 참조.
    # 외부에서 접속하는 주소 (예: https://hub.example.com). OpenAPI servers·안내 페이지에 사용
    public_url: str = ""
    # 팀 생성 정책: "admin_key" = X-Admin-Key 헤더가 HUB_ADMIN_KEY 와 같아야 함(비어 있으면 CLI로만 생성)
    #               "open"      = 누구나 팀 생성 가능
    team_creation: str = "admin_key"
    admin_key: str = ""
    max_body_chars: int = 30_000

    # ---- AI 깨우기 (wake) ----
    # 루틴 토큰·API 키를 DB에 암호화해 저장할 때 쓰는 키 (openssl rand -hex 32). 비우면 깨우기 설정 저장 불가
    secret_key: str = ""
    # 깨우기 기능 전체 on/off
    wake_enabled: bool = True
    # 앱 안에서 백그라운드 워커를 돌릴지 (테스트에서는 끔)
    wake_worker: bool = True
    wake_poll_seconds: float = 3.0
    # 시간대를 따로 정하지 않은 팀원의 기본 시간대 (활동 시간대·약속 시각 해석에 사용)
    default_timezone: str = "Asia/Seoul"
    # AI가 AI를 깨우는 연쇄의 최대 깊이. 사람/평소 AI가 쓴 글 = 0, 깨워진 AI가 쓴 글 = 1, ...
    wake_max_chain: int = 3
    # 한 스레드에 대리 응답기가 달 수 있는 최대 답글 수
    wake_thread_auto_cap: int = 6
    # MCP Events(ChatGPT) 콜백으로 사설·로컬 주소와 http 를 허용할지 (로컬 시험용. 운영에서는 false)
    events_allow_private: bool = False

    # ---- 대리 응답기 (EXAONE) ----
    # 팀원의 진짜 AI가 정한 시간 안에 답하지 않으면, 허브가 LG EXAONE 으로 그 AI를 대신해 답한다.
    # 키는 https://docs.exaone.ai 에서 발급 (무료, 30일마다 만료). HUB_EXAONE_API_KEY 또는 EXAONE_API_KEY
    exaone_api_key: str = Field("", validation_alias=AliasChoices("HUB_EXAONE_API_KEY", "EXAONE_API_KEY"))
    exaone_base_url: str = "https://api.exaone.ai/v1"
    exaone_model: str = "k-exaone_v2.5"
    # low / high / max. 대리 응답은 빨라야 하므로 low (max 는 생각이 길어 느림)
    exaone_reasoning_effort: str = "low"
    exaone_max_tokens: int = 4096
    # 하루(최근 24시간) 최대 호출 수. 무료 키는 하루 100회인 경우가 있어 조금 낮게
    exaone_daily_limit: int = 90
    # 대리 응답기 워커 on/off (테스트에서는 끔)
    proxy_worker: bool = True
    proxy_poll_seconds: float = 10.0
    # 팀원이 따로 정하지 않았을 때: 쪽지함 글은 몇 분, 실시간 세션 지목은 몇 초 기다린 뒤 대신 답할지
    proxy_inbox_after_minutes: int = 10
    proxy_live_after_seconds: int = 90
    # true 면 대리 응답 설정이 없는 팀원도 기본값으로 대신 답한다 (팀원 동의를 받은 팀에서만)
    proxy_all_members: bool = False
    # GitHub 저장소 읽기용 토큰 (선택. 비공개 저장소이거나 호출 한도를 늘릴 때). 읽기 권한만 주세요
    github_token: str = Field("", validation_alias=AliasChoices("HUB_GITHUB_TOKEN", "GITHUB_TOKEN"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
