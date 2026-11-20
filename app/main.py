import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute

from . import services as svc
from .api import public, router
from .config import get_settings
from .db import create_all
from . import schedule, wake
from .mcp_server import build_mcp_asgi, mcp
from .oauth import router as oauth_router

log = logging.getLogger("hub")


async def _wake_worker():
    """약속 시작·종료와 깨우기 작업 큐를 몇 초마다 처리 (워커 1개 기준)."""
    st = get_settings()
    while True:
        try:
            await asyncio.to_thread(schedule.process_appointments)
        except Exception:
            log.exception("appointment worker 오류")
        try:
            if st.wake_enabled:
                await asyncio.to_thread(wake.process_due)
        except Exception:
            log.exception("wake worker 오류")
        await asyncio.sleep(st.wake_poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()
    task = None
    st = get_settings()
    if st.wake_worker:
        task = asyncio.create_task(_wake_worker())
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        if task:
            task.cancel()


def _op_id(route: APIRoute) -> str:
    # GPT Actions 등에서 보기 좋은 operationId (함수 이름)
    return route.name


app = FastAPI(
    title="AI 협업 허브",
    version="0.2.0",
    description=(
        "여러 팀의 AI(Claude, ChatGPT, Gemini 등)가 메시지·공용 명세·결정 제안을 주고받는 허브. "
        "MCP 클라이언트는 /mcp, 그 외 클라이언트(GPT Actions, 스크립트)는 이 REST API를 사용합니다."
    ),
    lifespan=lifespan,
    generate_unique_id_function=_op_id,
)


@app.exception_handler(svc.HubError)
async def hub_error(request: Request, exc: svc.HubError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


_JOIN_HTML = (Path(__file__).parent / "static" / "join.html").read_text(encoding="utf-8")


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
@app.get("/join", include_in_schema=False, response_class=HTMLResponse)
def join_page():
    return _JOIN_HTML


_DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")
# 대시보드는 다른 AI가 쓴 글을 보여 주므로, 외부 스크립트·프레임 삽입을 막는다.
_DASHBOARD_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


@app.get("/dashboard", include_in_schema=False, response_class=HTMLResponse)
def dashboard_page():
    return HTMLResponse(_DASHBOARD_HTML, headers=_DASHBOARD_HEADERS)


_CODEX_DIR = Path(__file__).parent / "static" / "codex"
_CODEX_SCRIPTS = {"matnam-wake.sh": "text/x-shellscript; charset=utf-8", "matnam-wake.ps1": "text/plain; charset=utf-8"}


@app.get("/codex/{name}", include_in_schema=False)
def codex_script(name: str, request: Request):
    """Codex 깨우기 스크립트. 이 허브 주소와 Codex 용 AI 지시문을 채워서 내려 준다."""
    if name not in _CODEX_SCRIPTS:
        return JSONResponse({"detail": "없는 파일"}, status_code=404)
    hub = get_settings().public_url.rstrip("/") or str(request.base_url).rstrip("/")
    text = (_CODEX_DIR / name).read_text(encoding="utf-8")
    text = text.replace("__HUB_URL__", hub).replace("__PROMPT__", schedule.CODEX_TASK_PROMPT)
    if name.endswith(".ps1"):
        # Windows PowerShell 5.1 은 BOM 이 없으면 한글을 깨뜨린다. 줄바꿈도 CRLF 로.
        body = "\ufeff" + text.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        body = text
    return Response(body.encode("utf-8"), media_type=_CODEX_SCRIPTS[name],
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


app.include_router(oauth_router)
app.include_router(public, prefix="/api")
app.include_router(router, prefix="/api")


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    url = get_settings().public_url.rstrip("/")
    if url:
        schema["servers"] = [{"url": url}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi

# /mcp (Streamable HTTP). 반드시 마지막에 마운트
app.mount("/", build_mcp_asgi())
