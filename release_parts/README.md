# 合并 VideoLocalizer Windows 便携包分卷

本目录中的 `VideoLocalizer_Windows_x64.tar.zst.part001`、`part002` 等文件是便携发行包的二进制分卷。下载整个仓库后，在本目录运行 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\merge_windows.ps1
```

脚本会按顺序合并为 `VideoLocalizer_Windows_x64.tar.zst`，并自动校验 SHA-256。校验通过后，可用 Windows 11 自带 tar、7-Zip 或支持 zstd 的解压工具解压。
