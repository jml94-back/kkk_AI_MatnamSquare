"""대리 응답기가 근거로 읽는 GitHub 저장소 내용.

팀원이 등록한 저장소(owner/repo)에서 README 와, 질문 단어와 경로가 맞는 파일 몇 개를 읽어 온다.
스레드에 그 저장소 파일 링크(github.com/…/blob/…)가 있으면 그 파일도 읽는다.
허브는 저장소를 복사해 두지 않고, 읽은 결과만 잠깐(10분) 메모리에 캐시한다.
"""

import re
import time
from urllib.parse import quote

import httpx

from .config import get_settings
from .notes import _tokens

API = "https://api.github.com"
CACHE_SECONDS = 600
MAX_FILE_BYTES = 150_000
PER_FILE_CHARS = 5000
README_CHARS = 5000
TOTAL_CHARS = 18_000
MAX_PICKED = 3
MAX_REPOS = 5
TEXT_EXT = {
    ".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".cs", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".swift", ".rb", ".php", ".sh", ".ps1",
    ".sql", ".html", ".css", ".vue", ".svelte", ".gradle", ".xml", ".env.example", ".dockerfile",
}
NAME_RE = r"[A-Za-z0-9_.\-]{1,100}"
SPEC_RE = re.compile(rf"^({NAME_RE})/({NAME_RE})((?:/[^@\s]*)?)(?:@([\w./\-]{{1,100}}))?$")
URL_RE = re.compile(rf"^https://github\.com/({NAME_RE})/({NAME_RE})(?:/(?:tree|blob)/([\w.\-]+)(/[^\s?#]*)?)?/?(?:[?#].*)?$")
BLOB_RE = re.compile(rf"https://github\.com/({NAME_RE})/({NAME_RE})/blob/([\w.\-]+)/([^\s?#)\]]+)")

_cache: dict[str, tuple[float, object]] = {}


class RepoSpecError(ValueError):
    pass


def parse_repo(spec: str) -> dict:
    """'owner/repo', 'owner/repo/하위경로', 'owner/repo@branch', 또는 github.com URL."""
    s = (spec or "").strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    m = URL_RE.match(s)
    if m:
        owner, repo, branch, path = m.group(1), m.group(2), m.group(3), (m.group(4) or "")
    else:
        m = SPEC_RE.match(s)
        if not m:
            raise RepoSpecError(f"저장소 형식이 아닙니다: {spec[:80]} (예: owner/repo, owner/repo@main, owner/repo/docs)")
        owner, repo, path, branch = m.group(1), m.group(2), m.group(3), m.group(4)
    path = path.strip("/")
    if ".." in path.split("/"):
        raise RepoSpecError("경로에 .. 은 쓸 수 없습니다")
    out = f"{owner}/{repo}" + (f"/{path}" if path else "") + (f"@{branch}" if branch else "")
    return {"owner": owner, "repo": repo, "branch": branch, "path": path, "spec": out}


def clean_repos(specs: list[str] | None) -> list[str]:
    out = []
    for s in specs or []:
        if s and s.strip():
            out.append(parse_repo(s)["spec"])
    if len(out) > MAX_REPOS:
        raise RepoSpecError(f"저장소는 {MAX_REPOS}개까지 등록할 수 있습니다")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ HTTP (테스트에서 바꿔 끼움)
def http_get(url: str, *, headers: dict, timeout: float = 20) -> httpx.Response:
    return httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)


def _headers(raw: bool = False) -> dict:
    h = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-hub-proxy"}
    tok = get_settings().github_token
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    val = fn()
    _cache[key] = (time.monotonic(), val)
    if len(_cache) > 500:
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
            _cache.pop(k, None)
    return val


def _get_json(url: str):
    r = http_get(url, headers=_headers())
    if r.status_code != 200:
        raise RuntimeError(f"GitHub {r.status_code}")
    return r.json()


def _default_branch(owner: str, repo: str) -> str:
    return _cached(f"br:{owner}/{repo}", lambda: _get_json(f"{API}/repos/{owner}/{repo}").get("default_branch", "main"))


def _tree(owner: str, repo: str, branch: str) -> list[dict]:
    def load():
        j = _get_json(f"{API}/repos/{owner}/{repo}/git/trees/{quote(branch, safe='')}?recursive=1")
        return [{"path": t["path"], "size": t.get("size", 0)} for t in j.get("tree", []) if t.get("type") == "blob"]

    return _cached(f"tree:{owner}/{repo}@{branch}", load)


def _file(owner: str, repo: str, branch: str, path: str) -> str:
    def load():
        url = f"{API}/repos/{owner}/{repo}/contents/{quote(path)}?ref={quote(branch, safe='')}"
        r = http_get(url, headers=_headers(raw=True))
        if r.status_code != 200:
            raise RuntimeError(f"GitHub {r.status_code}")
        return r.text

    return _cached(f"file:{owner}/{repo}@{branch}:{path}", load)


def _is_text(path: str) -> bool:
    low = path.lower()
    name = low.rsplit("/", 1)[-1]
    if name in {"dockerfile", "makefile", "readme", "license"}:
        return True
    return any(low.endswith(e) for e in TEXT_EXT)


def _score(path: str, toks: set[str]) -> int:
    low = path.lower()
    parts = set(re.split(r"[/_.\-]", low))
    s = sum(3 * (t in parts) + (t in low) for t in toks if len(t) >= 3)
    if s and low.endswith(".md"):
        s += 1
    return s


def context_for(specs: list[str], question: str, thread_text: str = "") -> tuple[str, list[str]]:
    """(프롬프트에 넣을 저장소 자료, 문제 목록). 실패해도 예외 없이 빈 자료를 돌려준다."""
    if not specs:
        return "", []
    toks = _tokens(question)
    chunks, problems, used = [], [], 0
    allowed = {}
    for spec in specs[:MAX_REPOS]:
        try:
            r = parse_repo(spec)
        except RepoSpecError as e:
            problems.append(str(e))
            continue
        o, n = r["owner"], r["repo"]
        try:
            br = r["branch"] or _default_branch(o, n)
            allowed[(o.lower(), n.lower())] = (o, n)
            files = [f for f in _tree(o, n, br) if f["size"] <= MAX_FILE_BYTES and _is_text(f["path"])]
        except Exception as e:  # 네트워크·권한 문제는 답을 막지 않는다
            problems.append(f"{r['spec']}: 읽지 못함 ({e})"[:200])
            continue
        if r["path"]:
            files = [f for f in files if f["path"] == r["path"] or f["path"].startswith(r["path"] + "/")]
        base = (r["path"] + "/") if r["path"] else ""
        readme = next((f["path"] for f in files if f["path"].lower() in (base.lower() + "readme.md", base.lower() + "readme")), None)
        picked = []
        if readme:
            picked.append((readme, README_CHARS))
        ranked = sorted(((s, f["path"]) for f in files if f["path"] != readme and (s := _score(f["path"], toks)) > 0),
                        key=lambda x: (-x[0], len(x[1])))
        picked += [(p, PER_FILE_CHARS) for _, p in ranked[:MAX_PICKED]]
        paths = [f["path"] for f in files]
        tree_list = "\n".join(paths[:150]) + ("\n…" if len(paths) > 150 else "")
        chunks.append(f"===== 저장소 {o}/{n}@{br}{' /' + r['path'] if r['path'] else ''} 파일 목록 =====\n{tree_list}")
        for path, cap in picked:
            if used >= TOTAL_CHARS:
                break
            try:
                text = _file(o, n, br, path)[:cap]
            except Exception as e:
                problems.append(f"{o}/{n}/{path}: 읽지 못함 ({e})"[:200])
                continue
            used += len(text)
            chunks.append(f"===== 파일 {o}/{n}/{path} =====\n{text}")
    # 스레드에 등록된 저장소의 파일 링크가 있으면 그 파일도 (등록 안 된 저장소는 읽지 않음)
    for o, n, br, path in BLOB_RE.findall(thread_text or "")[:3]:
        key = (o.lower(), n.lower())
        if key not in allowed or used >= TOTAL_CHARS or ".." in path.split("/"):
            continue
        o, n = allowed[key]
        try:
            text = _file(o, n, br, path)[:PER_FILE_CHARS]
        except Exception as e:
            problems.append(f"{o}/{n}/{path}: 읽지 못함 ({e})"[:200])
            continue
        used += len(text)
        chunks.append(f"===== 파일 {o}/{n}/{path} (스레드 링크) =====\n{text}")
    return "\n\n".join(chunks), problems
