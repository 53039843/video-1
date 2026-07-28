$ErrorActionPreference='Stop'
$dist=(Resolve-Path (Join-Path $PSScriptRoot '..\dist\VideoLocalizer_Windows_x64')).Path
$runtime=Join-Path $dist 'runtime'
$app=Join-Path $dist 'app'
$tools=Join-Path $dist 'tools'
$data=Join-Path $dist 'data'
$port=8887
$env:VIDEO_LOCALIZER_PORT="$port"
$env:VIDEO_LOCALIZER_PORTABLE_ROOT=$dist
$env:VIDEO_LOCALIZER_DATA_DIR=$data
$env:VIDEO_LOCALIZER_RUNTIME_ROOT=$runtime
$env:VIDEO_LOCALIZER_FORCE_CPU='1'
$env:VIDEO_LOCALIZER_ENV_FILE=(Join-Path $dist 'config\.env.local')
$env:PATH="$tools;$runtime;$(Join-Path $runtime 'Scripts')"
$python=Join-Path $runtime 'python.exe'
$report=[ordered]@{dist=$dist;launcher_exists=(Test-Path (Join-Path $dist 'VideoLocalizer.exe'));runtime_python_exists=(Test-Path $python);ffmpeg_exists=(Test-Path (Join-Path $tools 'ffmpeg.exe'));ffprobe_exists=(Test-Path (Join-Path $tools 'ffprobe.exe'));model_exists=(Test-Path (Join-Path $app 'test_run_dl\models\faster-whisper-tiny\model.bin'));source_tree_not_used=$true}

$importCheck=Join-Path $app 'portable_import_check.py'
$importOut=& $python $importCheck 2>&1
$report.import_exit=$LASTEXITCODE
$report.import_output=($importOut -join "`n")

$outLog=Join-Path $dist 'logs\portable_validation_stdout.log'
$errLog=Join-Path $dist 'logs\portable_validation_stderr.log'
$proc=Start-Process -FilePath $python -ArgumentList '-u','local_app_v2.py' -WorkingDirectory $app -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru -WindowStyle Hidden
try{
 $ok=$false
 for($i=0;$i -lt 60;$i++){
  Start-Sleep -Milliseconds 500
  if($proc.HasExited){break}
  try{$health=Invoke-RestMethod "http://127.0.0.1:$port/api/health" -TimeoutSec 2; if($health.status -eq 'ok'){$ok=$true;break}}catch{}
 }
 $report.service_started=$ok
 $report.service_pid=$proc.Id
 if($ok){
  $report.health=$health
  $page=Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
  $report.page_status=$page.StatusCode
  $report.page_has_douyin=($page.Content -match '抖音')
  $report.data_db_created=(Test-Path (Join-Path $data 'local_jobs.sqlite3'))
  $report.database_in_data=([string]$health.output_dir).StartsWith($data,[System.StringComparison]::OrdinalIgnoreCase)
 }
} finally {
 if(-not $proc.HasExited){Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue}
}
$report.launcher_size=(Get-Item (Join-Path $dist 'VideoLocalizer.exe')).Length
$report.file_count=(Get-ChildItem $dist -Recurse -File).Count
$report.total_bytes=(Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum
$report.passed=($report.launcher_exists -and $report.runtime_python_exists -and $report.ffmpeg_exists -and $report.model_exists -and $report.import_exit -eq 0 -and $report.service_started -and $report.page_status -eq 200 -and $report.data_db_created -and $report.database_in_data)
$path=Join-Path $dist 'portable_validation.json'
$report | ConvertTo-Json -Depth 8 | Set-Content $path -Encoding UTF8
$report | ConvertTo-Json -Depth 8
if(-not $report.passed){exit 1}
