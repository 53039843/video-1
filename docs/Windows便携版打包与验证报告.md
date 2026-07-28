# Video Localizer Windows 便携版打包与验证报告

## 交付概述

程序已经打包为 **Windows x64 便携发行版**。用户只需完整解压文件夹并双击 `VideoLocalizer.exe`；启动器会展示系统与 GPU 检测结果、实际 URL、服务日志，并自动打开浏览器。目标电脑不需要预先安装 Python、FastAPI、faster-whisper、FFmpeg 或 CUDA Toolkit。

| 交付项 | 结果 |
|---|---|
| 原生 Windows EXE 启动器 | 已生成，14.5 KB |
| 便携 Python 3.11 运行时 | 已包含 |
| 本地 faster-whisper 模型 | 已包含 |
| FFmpeg / FFprobe | 已包含 |
| CUDA 12 cuBLAS / cuDNN / NVRTC | 已包含 |
| Edge TTS | 已包含并验证模块导入 |
| GPU/CPU 自动兼容 | 已实现 |
| NVENC/libx264 自动回退 | 已实现 |
| 独立数据目录与SQLite持久化 | 已实现 |
| AI密钥配置模板 | 已包含，真实密钥未打包 |

## 启动器能力

启动器使用 .NET Framework 原生 WinForms 编译，不依赖 Python 即可显示窗口和执行环境检查。启动时先验证 64 位 Windows、便携运行时、模型及媒体工具；随后识别 NVIDIA GPU、选择端口、注入便携 PATH 和数据目录，启动后端并轮询健康接口。服务成功后，启动器持续显示 URL 和日志，用户可以重新打开浏览器或停止服务。

| 检测或异常 | 行为 |
|---|---|
| 8790 已运行本程序 | 复用现有服务，不重复启动 |
| 8790 被其他程序占用 | 自动选择8791–8899 |
| 无NVIDIA GPU | 设置CPU兼容模式 |
| CUDA ASR失败 | 后端记录原因并回退CPU INT8 |
| NVENC失败 | 自动删除半成品并使用libx264重试 |
| 服务45秒未就绪 | 窗口显示失败原因和日志路径 |
| 浏览器启动失败 | URL仍保留在窗口中供复制 |

## 冷启动验证

冷启动回归显式使用发行目录中的 `runtime\python.exe`、`tools\ffmpeg.exe`、本地模型和 `data` 目录，没有调用开发虚拟环境。后端依赖导入、健康接口、首页、抖音页面元素和SQLite创建均通过。随后执行了真实 EXE 双击等价测试，启动器正确检测 RTX 3050、便携 FFmpeg，并在约 5 秒内启动本地服务。

| 指标 | 实测结果 |
|---|---|
| 便携依赖导入 | 通过 |
| 本地模型存在 | 通过 |
| CPU编码自动选择测试 | `libx264` |
| 便携后端启动 | 通过 |
| 健康接口 | `status = ok` |
| 首页HTTP状态 | 200 |
| 抖音功能入口 | 存在 |
| SQLite创建位置 | `data\local_jobs.sqlite3` |
| EXE实测URL | `http://127.0.0.1:8790/` |
| EXE启动窗口响应 | 正常 |
| 综合验证 | `passed = true` |

## 体积与发行选择

未压缩发行目录约 **3.07 GB**（约 2.86 GiB），共约 7,000 个文件。体积主要来自 GPU 推理运行库和完整版 FFmpeg。若删除 CUDA 目录，CPU版可显著变小，但将失去“自动识别并直接使用本地 NVIDIA GPU”的交付目标；因此本次保留完整GPU/CPU双路径。

> 该发行版采用“EXE启动器 + 同目录便携资源”，而非单文件自解压EXE。对于数GB模型与CUDA依赖，这是启动最快、更新最容易、发生错误时最可诊断的结构。单文件方案每次启动都需要解压大量文件，不符合最高效运行要求。

## 安全与限制

启动器为本机编译且未使用商业代码签名证书，Windows 可能显示 SmartScreen 提示。AI 网关真实密钥没有写入发行包；`config\.env.local` 仅包含空白模板。控制面板只监听 `127.0.0.1`，不会默认暴露到局域网。支持范围为 Windows 10/11 64 位；不保证 Windows 7、32 位系统、ARM Windows 或过旧的 NVIDIA 驱动。
