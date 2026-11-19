from functools import lru_cache

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
    # AI가 AI를 깨우는 연쇄의 최대 깊이. 사람/평소 AI가 쓴 글 = 0, 깨워진 AI가 쓴 글 = 1, ...
    wake_max_chain: int = 3
    # 한 스레드에 대리 응답기가 달 수 있는 최대 답글 수
    wake_thread_auto_cap: int = 6


@lru_cache
def get_settings() -> Settings:
    return Settings()
