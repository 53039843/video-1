$ErrorActionPreference='Stop'
$root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dist=Join-Path $root 'dist\VideoLocalizer_Windows_x64'
$runtime=Join-Path $dist 'runtime'
$app=Join-Path $dist 'app'
$tools=Join-Path $dist 'tools'
$config=Join-Path $dist 'config'
$data=Join-Path $dist 'data'
$logs=Join-Path $dist 'logs'
$basePython='C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none'
$venv=Join-Path $root '.venv311'

if(Test-Path $dist){Remove-Item $dist -Recurse -Force}
New-Item -ItemType Directory -Force -Path $dist,$runtime,$app,$tools,$config,$data,$logs,(Join-Path $data 'downloads'),(Join-Path $data 'output'),(Join-Path $data 'local_jobs') | Out-Null

Copy-Item (Join-Path $root 'packaging\build\VideoLocalizer.exe') (Join-Path $dist 'VideoLocalizer.exe')
robocopy $basePython $runtime /E /NFL /NDL /NJH /NJS /NP /XD '__pycache__' /XF '*.pyc' | Out-Null
if($LASTEXITCODE -ge 8){throw "复制Python基础运行时失败：$LASTEXITCODE"}

$site=Join-Path $runtime 'Lib\site-packages'
$sourceSite=Join-Path $venv 'Lib\site-packages'
$packages=@(
 'annotated_doc','annotated_doc-*.dist-info','annotated_types','annotated_types-*.dist-info','anyio','anyio-*.dist-info',
 'av','av.libs','av-*.dist-info','certifi','certifi-*.dist-info','charset_normalizer','charset_normalizer-*.dist-info','click','click-*.dist-info',
 'colorama','colorama-*.dist-info','ctranslate2','ctranslate2-*.dist-info','fastapi','fastapi-*.dist-info','faster_whisper','faster_whisper-*.dist-info',
 'filelock','filelock-*.dist-info','flatbuffers','flatbuffers-*.dist-info','fsspec','fsspec-*.dist-info','h11','h11-*.dist-info',
 'hf_xet','hf_xet-*.dist-info','httpcore','httpcore-*.dist-info','httptools','httptools-*.dist-info','httpx','httpx-*.dist-info',
 'huggingface_hub','huggingface_hub-*.dist-info','idna','idna-*.dist-info','numpy','numpy.libs','numpy-*.dist-info','onnxruntime','onnxruntime-*.dist-info',
 'cv2','opencv_python_headless.libs','opencv_python_headless-*.dist-info','packaging','packaging-*.dist-info','pydantic','pydantic-*.dist-info','pydantic_core','pydantic_core-*.dist-info',
 'python_multipart','python_multipart-*.dist-info','multipart','regex','regex-*.dist-info','requests','requests-*.dist-info',
 'setuptools','setuptools-*.dist-info','starlette','starlette-*.dist-info','tokenizers','tokenizers-*.dist-info','tqdm','tqdm-*.dist-info',
 'typing_extensions.py','typing_extensions-*.dist-info','typing_inspection','typing_inspection-*.dist-info','urllib3','urllib3-*.dist-info',
 'uvicorn','uvicorn-*.dist-info','watchfiles','watchfiles-*.dist-info','websockets','websockets-*.dist-info','yaml','PyYAML-*.dist-info',
 'edge_tts','edge_tts-*.dist-info','aiohttp','aiohttp-*.dist-info','aiohappyeyeballs','aiohappyeyeballs-*.dist-info','aiosignal','aiosignal-*.dist-info','attrs','attrs-*.dist-info','frozenlist','frozenlist-*.dist-info','multidict','multidict-*.dist-info','propcache','propcache-*.dist-info','tabulate','tabulate-*.dist-info','yarl','yarl-*.dist-info',
 'nvidia'
)
foreach($pattern in $packages){
 Get-ChildItem $sourceSite -Filter $pattern -Force -ErrorAction SilentlyContinue | ForEach-Object { Copy-Item $_.FullName $site -Recurse -Force }
}
Get-ChildItem $site -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $site -Recurse -File -Include '*.pyc','*.pyo' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
uv pip install --python (Join-Path $runtime 'python.exe') --break-system-packages --link-mode copy 'edge-tts==7.2.7' | Out-Null
if($LASTEXITCODE -ne 0){throw '安装便携Edge TTS失败'}

$edgeScript=Join-Path $runtime 'edge-tts.cmd'
$edgeCmd=@'
@echo off
"%~dp0python.exe" -m edge_tts %*
'@
$edgeCmd.TrimStart() | Set-Content $edgeScript -Encoding ASCII

foreach($name in @('local_app_v2.py','real_media_pipeline.py','subtitle_detector.py','control_panel_v3.html')){Copy-Item (Join-Path $root $name) (Join-Path $app $name)}
New-Item -ItemType Directory -Force (Join-Path $app 'test_run_dl\models') | Out-Null
robocopy (Join-Path $root 'test_run_dl\models\faster-whisper-tiny') (Join-Path $app 'test_run_dl\models\faster-whisper-tiny') /E /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -ge 8){throw "复制ASR模型失败：$LASTEXITCODE"}
New-Item -ItemType Directory -Force (Join-Path $app 'test_run_dl') | Out-Null
if(Test-Path (Join-Path $root 'test_run_dl\translation_cache.json')){Copy-Item (Join-Path $root 'test_run_dl\translation_cache.json') (Join-Path $app 'test_run_dl\translation_cache.json')}

$ffmpegTarget=@((Get-Item (Get-Command ffmpeg).Source -Force).Target)[0]
$ffprobeTarget=@((Get-Item (Get-Command ffprobe).Source -Force).Target)[0]
Copy-Item $ffmpegTarget (Join-Path $tools 'ffmpeg.exe')
Copy-Item $ffprobeTarget (Join-Path $tools 'ffprobe.exe')
$nvsmi=Get-Command nvidia-smi -ErrorAction SilentlyContinue
if($nvsmi){Copy-Item $nvsmi.Source (Join-Path $tools 'nvidia-smi.exe') -ErrorAction SilentlyContinue}

$envTemplate=@'
# 将实际AI网关密钥填写在等号后；不要把包含密钥的文件公开分享。
VIDEO_LOCALIZER_AI_API_KEY=
VIDEO_LOCALIZER_AI_BASE_URL=https://ai-api-gateway.app.baizhi.cloud/api/openai
VIDEO_LOCALIZER_AI_PRIMARY_MODEL=agnes-2.0-flash
VIDEO_LOCALIZER_AI_FALLBACK_MODEL=deepseek-v4-flash
'@
Set-Content (Join-Path $config '.env.local') $envTemplate -Encoding UTF8
Set-Content (Join-Path $data 'README.txt') '下载、输出、任务历史和SQLite数据库将保存在此目录。' -Encoding UTF8

Get-ChildItem $dist -Recurse -File | ForEach-Object {$_.IsReadOnly=$false}
$bytes=(Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum
$manifestPath=Join-Path $dist "build_manifest.json"
$manifest=[pscustomobject]@{Dist=$dist;Files=(Get-ChildItem $dist -Recurse -File).Count;Bytes=$bytes;GB=[math]::Round($bytes/1GB,2)}
$manifest | ConvertTo-Json | Set-Content $manifestPath -Encoding UTF8
Write-Output "DIST=$dist"
Write-Output "SIZE_GB=$([math]::Round($bytes/1GB,2))"
