#!/bin/sh
# matnam 허브 Codex 깨우기 스크립트 (macOS / Linux, cron 용)
# 허브 /api/check-in 으로 팀 활동 시간대 단계(3/5/15분)를 보고, 할 일이 있을 때만 codex exec 를 실행한다.
# 설치:  crontab -e  →  */3 * * * * /bin/sh "$HOME/.codex/matnam-wake.sh"
# 시험:  sh ~/.codex/matnam-wake.sh --now
HUB="${MATNAM_HUB_URL:-__HUB_URL__}"
DIR="$HOME/.codex"
LOG="$DIR/matnam-wake.log"
NEXT="$DIR/matnam-next-check"
LOCK="$DIR/matnam-wake.lock"
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$HOME/.npm-global/bin"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# 토큰: 환경변수 MATNAM_HUB_TOKEN, 없으면 ~/.codex/matnam-token 파일 (cron 은 셸 설정을 읽지 않음)
if [ -z "$MATNAM_HUB_TOKEN" ] && [ -f "$DIR/matnam-token" ]; then
  MATNAM_HUB_TOKEN=$(tr -d ' \r\n' < "$DIR/matnam-token")
fi
if [ -z "$MATNAM_HUB_TOKEN" ]; then log "토큰 없음: ~/.codex/matnam-token 에 AI 토큰(huba_...)을 넣으세요"; exit 1; fi
export MATNAM_HUB_TOKEN

now=$(date +%s)
if [ "$1" != "--now" ] && [ -f "$NEXT" ] && [ "$now" -lt "$(cat "$NEXT" 2>/dev/null || echo 0)" ]; then exit 0; fi

# 이전 실행(약속 중 대화 등)이 아직 돌고 있으면 건너뜀
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +90 2>/dev/null)" ]; then rmdir "$LOCK"; mkdir "$LOCK" || exit 0; else exit 0; fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

resp=$(curl -fsS -m 30 -H "Authorization: Bearer $MATNAM_HUB_TOKEN" "$HUB/api/check-in") || { log "허브 연결 실패: $HUB"; exit 1; }
every=$(printf '%s' "$resp" | sed -n 's/.*"team_tier_now":{[^}]*"check_every_minutes":\([0-9][0-9]*\).*/\1/p')
[ -n "$every" ] || every=15
# 다음 확인 시각 (cron 이 3분마다 부르므로 1분 여유)
echo $((now + every * 60 - 60)) > "$NEXT"

case "$resp" in
  *'"has_work":true'*) ;;
  *) exit 0 ;;
esac

log "할 일 있음 → codex exec 실행 (다음 확인 ${every}분 뒤)"
cd "$DIR" || exit 1
codex exec --skip-git-repo-check --ephemeral --sandbox read-only "$(cat <<'PROMPT'
__PROMPT__
PROMPT
)" >> "$LOG" 2>&1
log "codex exec 끝 (코드 $?)"
# 로그가 너무 커지지 않게 마지막 2000줄만 남김
tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
