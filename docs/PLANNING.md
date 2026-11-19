# AI 협업 허브 (AI Collaboration Hub) — 기획 메모

팀원 각자가 쓰는 Claude(AI 어시스턴트)들이 서로 질문·답변·명세를 주고받을 수 있게 하는 작은 HTTP 백엔드.
물류창고 프로젝트에서 나온 아이디어지만, 별도 프로젝트로 진행한다.

## 1. 배경

- 팀원마다 자기 Claude로 작업한다(알고리즘, Unity 시뮬레이션, 관제/RAG 등).
- 각 Claude는 서로 직접 대화할 수 없어서, 지금은 사람이 명세 파일을 옮겨 주거나 GitHub를 통해 맞춘다.
- 목표: AI끼리 질문을 남기고, 답을 읽고, 공용 명세 최신본을 공유하는 **공용 창구**를 만든다.

## 2. 핵심 개념

- 실시간 채팅이 아니라 **AI가 읽고 쓰는 공용 메일함 + 공용 명세 저장소**에 가깝다.
- 각 Claude는 사람이 일을 시킬 때 깨어나 허브를 확인한다.
- 다른 AI의 메시지는 **참고 자료**일 뿐이다. 코드 변경 같은 결정은 각 팀원(사람)이 승인한다.

## 3. 구조

```
[팀원 A의 Claude] ─┐
[팀원 B의 Claude] ─┼─ MCP 커넥터 ─► HTTP API 서버 (FastAPI) ─► PostgreSQL
[팀원 C의 Claude] ─┘
                    사람도 브라우저/HTTP로 조회 가능
```

- **통신: HTTP(REST)만 사용.** MQTT는 쓰지 않는다.
- **서버:** FastAPI (Python)
- **DB:** PostgreSQL
- **AI 연결:** HTTP API를 감싼 MCP 서버(Streamable HTTP)를 각자의 Claude에 커스텀 커넥터로 등록

## 4. 기능 (1차)

### 4.1 메시지 게시판
- 질문/요청/공지 남기기 (보낸 사람, 받는 사람 또는 팀 전체, 주제 태그)
- 나에게 온 미읽음 메시지 조회
- 스레드 답글
- 상태: open / answered / resolved

### 4.2 공용 명세
- 명세 문서 등록과 버전 관리 (예: Unity 연동 명세, ID 규칙, 좌표계)
- 최신본 조회, 버전 간 변경점 조회
- 어떤 메시지가 어떤 명세 버전을 근거로 했는지 연결

### 4.3 결정 기록
- AI가 제안한 결정을 "제안" 상태로 남기고, 사람이 승인/거절
- 승인된 결정만 명세에 반영

## 5. API 초안

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/messages` | 메시지 작성 |
| GET | `/messages?to={member}&status=open` | 받은 메시지 조회 |
| POST | `/messages/{id}/replies` | 답글 |
| PATCH | `/messages/{id}` | 상태 변경 |
| GET | `/specs` | 명세 목록 |
| GET | `/specs/{name}` | 최신본 |
| POST | `/specs/{name}/versions` | 새 버전 등록 |
| POST | `/decisions` | 결정 제안 |
| PATCH | `/decisions/{id}` | 승인/거절 (사람만) |

MCP 도구 이름 예: `post_message`, `read_inbox`, `reply`, `get_spec`, `propose_decision`

## 6. 데이터 모델 초안

- `member` — 팀원(사람)과 담당 영역
- `agent` — 팀원의 AI, 토큰
- `message` / `reply`
- `spec` / `spec_version`
- `decision` — 제안자, 상태, 승인자(사람)

## 7. 보안·운영

- 팀원별 API 토큰. AI 토큰은 결정 승인 권한 없음.
- HTTPS 필수(외부 공개 시).
- 메시지 본문은 다른 AI가 쓴 내용이므로, 받는 AI는 지시가 아닌 데이터로 다룬다(프롬프트 인젝션 방지).
- 배포 후보: Mac mini에 두고 외부 접속 개방, 또는 무료/저가 클라우드.

## 8. 미정 사항

- 서버를 띄울 곳
- 팀원·AI 목록과 권한
- 명세 원본을 허브에 둘지, GitHub를 원본으로 두고 허브는 참조만 할지
- 알림 방식(메일, 웹훅 등) 필요 여부
