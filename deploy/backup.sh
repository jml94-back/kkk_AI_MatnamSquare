#!/usr/bin/env bash
# 매일 DB 백업 (crontab 예: 0 4 * * * /home/pi/ai-hub/deploy/backup.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
docker compose exec -T db pg_dump -U hub hub | gzip > "backups/hub-$(date +%F).sql.gz"
# 14일 지난 백업 삭제
find backups -name 'hub-*.sql.gz' -mtime +14 -delete
