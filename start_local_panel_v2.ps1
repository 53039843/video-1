$ErrorActionPreference='Stop'
$root=Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python=Join-Path $root '.venv311\Scripts\python.exe'
if(-not(Test-Path $python)){throw '未找到 .venv311，请先创建 Python 3.11 虚拟环境'}
$old=Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -match 'video_localizer.*local_app_v2.py'}
foreach($p in $old){Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue}
Start-Process -FilePath $python -ArgumentList '.\local_app_v2.py' -WorkingDirectory $root
Start-Sleep -Seconds 3
$health=Invoke-RestMethod 'http://127.0.0.1:8790/api/health'
if($health.status -ne 'ok'){throw '后端健康检查失败'}
Start-Process 'http://127.0.0.1:8790/'
Write-Host 'Video Localizer 已启动： http://127.0.0.1:8790/' -ForegroundColor Green
