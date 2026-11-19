"""테스트용 가짜 AI: 받은 글을 읽고 REST로 답한다 (감시기가 실행)."""
import json
import os
import sys
import urllib.request

URL, TOKEN = os.environ["HUB_URL"], os.environ["HUB_AGENT_TOKEN"]


def call(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


prompt = sys.argv[1]
assert os.path.exists(os.environ["HUB_MCP_CONFIG"])
for item in call("GET", "/api/inbox"):
    if f"#{item['id']}(" in prompt:
        call("GET", f"/api/messages/{item['id']}")
        call("POST", f"/api/messages/{item['id']}/replies", {"body": "자동 답변입니다"})
print("done")
