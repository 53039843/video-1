<<<<<<< HEAD
# video-1
抖音视频去水印到翻译字幕自动化
=======
﻿# Video Localizer Windows

Video Localizer 是一个本地 Windows 视频双字幕与语言转换控制面板。程序支持本地视频上传、抖音链接解析下载、原硬字幕保留、目标语言译文烧录、实时日志、任务持久化和便携版 EXE 启动。

## 快速使用

推荐直接下载 Release 中的 `VideoLocalizer_Windows_x64.tar.zst`，完整解压后双击 `VideoLocalizer.exe`。启动窗口会自动检测 Windows、NVIDIA GPU、FFmpeg、端口和运行时，并展示本地控制面板 URL。默认入口通常为 `http://127.0.0.1:8790/`。

## 重要说明

仓库不包含用户的 `.env.local`、历史任务数据库、下载视频或输出视频。AI 网关密钥应复制 `config/.env.local.example` 为 `config/.env.local` 后自行填写。便携发行包包含 Python 运行时、FFmpeg、本地 ASR 模型和 GPU/CPU 兼容依赖。

## 主要能力

| 功能 | 状态 |
|---|---|
| 点击或拖拽选择视频 | 支持 |
| 抖音链接解析下载 | 支持 |
| 默认马来语译文烧录 | 支持 |
| 原硬字幕保留并在下方叠加译文 | 支持 |
| 字号避让与时长跟随 | 支持 |
| 实时日志与SQLite持久化 | 支持 |
| 双击 EXE 启动控制面板 | 支持 |

## 发行包

完整便携版请在 GitHub Release 下载。源码仅用于维护与审计，不建议直接从源码目录运行生产任务。
>>>>>>> 87ae192 (Initial source and packaging documentation)
