$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir
$out = 'VideoLocalizer_Windows_x64.tar.zst'
Remove-Item $out -Force -ErrorAction SilentlyContinue
$parts = Get-ChildItem 'VideoLocalizer_Windows_x64.tar.zst.part*' -File | Sort-Object Name
if($parts.Count -eq 0){ throw '未找到分卷文件' }
$target = [System.IO.File]::Create((Join-Path $dir $out))
try {
  foreach($p in $parts){
    Write-Host "合并 $($p.Name)"
    $src = [System.IO.File]::OpenRead($p.FullName)
    try { $src.CopyTo($target) } finally { $src.Close() }
  }
} finally { $target.Close() }
$shaLine = Get-Content '.\VideoLocalizer_Windows_x64.tar.zst.sha256.txt' -Raw
$expected = (($shaLine -split '\s+') | Where-Object { $_ -match '^[A-Fa-f0-9]{64}$' } | Select-Object -First 1).ToUpperInvariant()
$actual = (Get-FileHash $out -Algorithm SHA256).Hash.ToUpperInvariant()
Write-Host "Expected: $expected"
Write-Host "Actual:   $actual"
if($expected -ne $actual){ throw 'SHA-256 校验失败，请重新下载分卷' }
Write-Host '合并完成，SHA-256 校验通过。'
