# matnam 허브 Codex 깨우기 스크립트 (Windows, 작업 스케줄러 용)
# 허브 /api/check-in 으로 팀 활동 시간대 단계(3/5/15분)를 보고, 할 일이 있을 때만 codex exec 를 실행한다.
# 시험:  powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.codex\matnam-wake.ps1" -Now
param([switch]$Now)
$ErrorActionPreference = "Stop"
$Hub = if ($env:MATNAM_HUB_URL) { $env:MATNAM_HUB_URL } else { "__HUB_URL__" }
$Dir = Join-Path $env:USERPROFILE ".codex"
$Log = Join-Path $Dir "matnam-wake.log"
$Next = Join-Path $Dir "matnam-next-check"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function Log($m) { Add-Content -Path $Log -Encoding UTF8 -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m) }

# 토큰: 환경변수 MATNAM_HUB_TOKEN, 없으면 %USERPROFILE%\.codex\matnam-token 파일
if (-not $env:MATNAM_HUB_TOKEN) {
  $tf = Join-Path $Dir "matnam-token"
  if (Test-Path $tf) { $env:MATNAM_HUB_TOKEN = (Get-Content $tf -Raw).Trim() }
}
if (-not $env:MATNAM_HUB_TOKEN) { Log "토큰 없음: .codex\matnam-token 에 AI 토큰(huba_...)을 넣으세요"; exit 1 }

$nowTs = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
if (-not $Now -and (Test-Path $Next)) {
  $n = 0; [long]::TryParse((Get-Content $Next -Raw).Trim(), [ref]$n) | Out-Null
  if ($nowTs -lt $n) { exit 0 }
}

try {
  $r = Invoke-RestMethod -Uri "$Hub/api/check-in" -Headers @{ Authorization = "Bearer $($env:MATNAM_HUB_TOKEN)" } -TimeoutSec 30
} catch { Log "허브 연결 실패: $Hub ($($_.Exception.Message))"; exit 1 }
$every = 15
if ($r.team_tier_now -and $r.team_tier_now.check_every_minutes) { $every = [int]$r.team_tier_now.check_every_minutes }
# 다음 확인 시각 (작업 스케줄러가 3분마다 부르므로 1분 여유)
Set-Content -Path $Next -Value ($nowTs + $every * 60 - 60)
if (-not $r.has_work) { exit 0 }

$prompt = @'
__PROMPT__
'@
Log "할 일 있음 → codex exec 실행 (다음 확인 $every분 뒤)"
Set-Location $Dir
# 작업 스케줄러는 같은 작업이 돌고 있으면 새로 시작하지 않으므로 약속 중 긴 대화도 겹치지 않는다
$ErrorActionPreference = "Continue"
& codex exec --skip-git-repo-check --ephemeral --sandbox read-only $prompt *>> $Log
Log "codex exec 끝 (코드 $LASTEXITCODE)"
$lines = Get-Content $Log -Encoding UTF8 -Tail 2000
Set-Content -Path $Log -Encoding UTF8 -Value $lines
