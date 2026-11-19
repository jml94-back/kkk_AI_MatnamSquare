import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

from . import services as svc
from .api import public, router
from .config import get_settings
from .db import create_all
from . import wake
from .mcp_server import build_mcp_asgi, mcp

log = logging.getLogger("hub")


async def _wake_worker():
    """깨우기 작업 큐를 몇 초마다 처리 (워커 1개 기준)."""
    st = get_settings()
    while True:
        try:
            await asyncio.to_thread(wake.process_due)
        except Exception:
            log.exception("wake worker 오류")
        await asyncio.sleep(st.wake_poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()
    task = None
    st = get_settings()
    if st.wake_enabled and st.wake_worker:
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
