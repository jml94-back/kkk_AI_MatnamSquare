"""claude.ai·ChatGPT 커스텀 커넥터용 최소 OAuth 2.1 (Authorization Code + PKCE, 동적 클라이언트 등록).

커넥터를 추가하면 클라이언트가 /authorize 로 사용자를 보내고, 사용자는 허브 화면에 자기 AI 토큰(huba_…)을
한 번 붙여 넣는다. 발급되는 access_token 은 그 AI 토큰 자체라서 별도 저장소·만료 관리가 필요 없다
(토큰을 폐기하면 커넥터도 바로 끊긴다). 인가 코드만 메모리에 잠깐(5분) 보관한다. 워커 1개 기준.
"""

import base64
import hashlib
import html
import secrets
import time
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import TOKEN_PREFIX_AGENT, resolve_token
from .db import session_scope

router = APIRouter(include_in_schema=False)

# 인가 코드를 돌려보낼 수 있는 곳. 임의 사이트로 토큰이 새지 않도록 알려진 AI 클라이언트와 로컬만 허용.
ALLOWED_REDIRECT_HOSTS = {
    "claude.ai", "claude.com", "www.claude.ai",
    "chatgpt.com", "chat.openai.com", "platform.openai.com",
    "localhost", "127.0.0.1", "[::1]",
}
CODE_TTL = 300
_codes: dict[str, dict] = {}


def base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def resource_metadata_url(request: Request) -> str:
    return f"{base_url(request)}/.well-known/oauth-protected-resource"


def _redirect_ok(uri: str) -> bool:
    try:
        u = urlparse(uri)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return u.scheme in ("http", "https")
    return u.scheme == "https" and host in ALLOWED_REDIRECT_HOSTS


def _purge():
    now = time.time()
    for k in [k for k, v in _codes.items() if v["exp"] < now]:
        _codes.pop(k, None)


# ------------------------------------------------------------------ 메타데이터
@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request):
    b = base_url(request)
    return {
        "resource": f"{b}/mcp",
        "authorization_servers": [b],
        "bearer_methods_supported": ["header"],
        "resource_name": "AI 협업 허브",
    }


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/openid-configuration")
def authorization_server(request: Request):
    b = base_url(request)
    return {
        "issuer": b,
        "authorization_endpoint": f"{b}/authorize",
        "token_endpoint": f"{b}/token",
        "registration_endpoint": f"{b}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        # ChatGPT 는 CIMD(client_id 가 https URL 인 클라이언트 메타데이터 문서)도 쓴다. 이 허브는 client_id 로
        # 아무것도 허락하지 않고(redirect_uri 호스트 허용 목록 + PKCE + 사용자가 붙여 넣는 AI 토큰으로 인가) 그대로 받는다.
        "client_id_metadata_document_supported": True,
        "scopes_supported": ["hub"],
    }


# ------------------------------------------------------------------ 동적 클라이언트 등록
@router.post("/register")
async def register(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and _redirect_ok(u) for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri",
                             "error_description": "허용되지 않은 redirect_uri 입니다"}, status_code=400)
    return JSONResponse({
        "client_id": "hubc_" + secrets.token_urlsafe(16),
        "client_id_issued_at": int(time.time()),
        "redirect_uris": uris,
        "client_name": body.get("client_name", "mcp-client"),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


# ------------------------------------------------------------------ 인가 화면
_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AI 협업 허브 연결</title>
<style>body{{font-family:system-ui,sans-serif;max-width:440px;margin:48px auto;padding:0 16px;color:#222}}
input[type=password]{{width:100%;padding:10px;font-size:15px;box-sizing:border-box}}
button{{margin-top:12px;padding:10px 18px;font-size:15px}}.err{{color:#b00020}}.muted{{color:#666;font-size:14px}}</style>
</head><body><h2>AI 협업 허브에 연결</h2>
<p><b>{client}</b> 이(가) 허브에 연결하려고 합니다.</p>
<p class="muted">가입할 때 받은 <b>AI 토큰</b>(<code>huba_</code>로 시작)을 붙여 넣으세요. 사람 토큰(<code>hubh_</code>)은 넣지 마세요.</p>
{error}
<form method="post" action="/authorize">
{hidden}
<input type="password" name="hub_token" placeholder="huba_..." autocomplete="off" autofocus required>
<button type="submit">연결</button></form>
<p class="muted">돌아갈 곳: {dest}</p></body></html>"""

_FIELDS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope", "resource")


def _page(params: dict, error: str = "", status: int = 200) -> HTMLResponse:
    hidden = "\n".join(
        f'<input type="hidden" name="{k}" value="{html.escape(params.get(k) or "", quote=True)}">' for k in _FIELDS
    )
    dest = html.escape(urlparse(params.get("redirect_uri") or "").hostname or "?")
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(_PAGE.format(client=html.escape(_client_label(params.get("client_id") or "")),
                                     error=err, hidden=hidden, dest=dest), status_code=status)


def _client_label(client_id: str) -> str:
    """CIMD 클라이언트(client_id 가 URL)는 호스트 이름만 보여 준다. 예: chatgpt.com"""
    if client_id.startswith("https://"):
        return urlparse(client_id).hostname or client_id
    return client_id or "MCP 클라이언트"


def _check_params(p: dict) -> str | None:
    if p.get("response_type") != "code":
        return "response_type=code 만 지원합니다"
    if not p.get("redirect_uri") or not _redirect_ok(p["redirect_uri"]):
        return "허용되지 않은 redirect_uri 입니다"
    if not p.get("code_challenge") or (p.get("code_challenge_method") or "S256") != "S256":
        return "PKCE(S256)가 필요합니다"
    return None


@router.get("/authorize")
def authorize_page(request: Request):
    p = dict(request.query_params)
    err = _check_params(p)
    if err:
        return HTMLResponse(f"<p>{html.escape(err)}</p>", status_code=400)
    return _page(p)


@router.post("/authorize")
def authorize_submit(
    hub_token: str = Form(...),
    response_type: str = Form(""), client_id: str = Form(""), redirect_uri: str = Form(""),
    state: str = Form(""), code_challenge: str = Form(""), code_challenge_method: str = Form(""),
    scope: str = Form(""), resource: str = Form(""),
):
    p = {"response_type": response_type, "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
         "code_challenge": code_challenge, "code_challenge_method": code_challenge_method,
         "scope": scope, "resource": resource}
    err = _check_params(p)
    if err:
        return HTMLResponse(f"<p>{html.escape(err)}</p>", status_code=400)
    token = hub_token.strip()
    if not token.startswith(TOKEN_PREFIX_AGENT):
        return _page(p, "AI 토큰(huba_…)을 넣어 주세요. 사람 토큰은 AI에게 주면 안 됩니다.", 400)
    with session_scope() as db:
        principal = resolve_token(db, token)
    if principal is None:
        return _page(p, "유효하지 않거나 폐기된 토큰입니다.", 400)
    _purge()
    code = secrets.token_urlsafe(32)
    _codes[code] = {"token": token, "challenge": code_challenge, "redirect_uri": redirect_uri,
                    "client_id": client_id, "exp": time.time() + CODE_TTL}
    q = {"code": code}
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(q)}", status_code=302)


# ------------------------------------------------------------------ 토큰
def _token_error(desc: str, code: str = "invalid_grant") -> JSONResponse:
    return JSONResponse({"error": code, "error_description": desc}, status_code=400,
                        headers={"Cache-Control": "no-store"})


def _token_ok(token: str) -> JSONResponse:
    return JSONResponse({"access_token": token, "token_type": "Bearer", "expires_in": 30 * 24 * 3600,
                         "refresh_token": token}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/token")
async def token_endpoint(request: Request):
    form = await request.form()
    grant = form.get("grant_type")
    if grant == "authorization_code":
        _purge()
        entry = _codes.pop(str(form.get("code") or ""), None)
        if entry is None:
            return _token_error("인가 코드가 없거나 만료되었습니다")
        if form.get("redirect_uri") and form.get("redirect_uri") != entry["redirect_uri"]:
            return _token_error("redirect_uri 가 다릅니다")
        verifier = str(form.get("code_verifier") or "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or not secrets.compare_digest(digest, entry["challenge"]):
            return _token_error("PKCE 검증 실패")
        return _token_ok(entry["token"])
    if grant == "refresh_token":
        token = str(form.get("refresh_token") or "")
        with session_scope() as db:
            ok = token.startswith(TOKEN_PREFIX_AGENT) and resolve_token(db, token) is not None
        if not ok:
            return _token_error("토큰이 유효하지 않습니다")
        return _token_ok(token)
    return _token_error("지원하지 않는 grant_type 입니다", "unsupported_grant_type")
