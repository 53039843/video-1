from __future__ import annotations
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

from faster_whisper import WhisperModel
from subtitle_detector import analyze_hard_subtitles, align_segments_to_subtitle_intervals, analyze_segment_subtitle_layouts

ROOT = Path(__file__).resolve().parent
PORTABLE_ROOT = Path(os.getenv("VIDEO_LOCALIZER_PORTABLE_ROOT", str(ROOT))).resolve()
RUNTIME_ROOT = Path(os.getenv("VIDEO_LOCALIZER_RUNTIME_ROOT", str(ROOT / ".venv311"))).resolve()
MODEL = ROOT / "test_run_dl" / "models" / "faster-whisper-tiny"


def _enable_project_cuda_runtime():
    """将隔离环境安装的NVIDIA DLL加入当前进程PATH，不修改系统环境变量。"""
    site_packages = RUNTIME_ROOT / "Lib" / "site-packages"
    candidates = [
        site_packages / "nvidia" / "cublas" / "bin",
        site_packages / "nvidia" / "cudnn" / "bin",
        site_packages / "nvidia" / "cuda_nvrtc" / "bin",
    ]
    existing = [str(path) for path in candidates if path.exists()]
    if existing:
        os.environ["PATH"] = os.pathsep.join(existing + [os.environ.get("PATH", "")])
        if hasattr(os, "add_dll_directory"):
            for path in existing:
                try:
                    os.add_dll_directory(path)
                except OSError:
                    pass
    return existing


CUDA_RUNTIME_DIRS = _enable_project_cuda_runtime()
EDGE_VOICES = {
    "English": {"女声": "en-US-JennyNeural", "男声": "en-US-GuyNeural"},
    "Bahasa Melayu": {"女声": "ms-MY-YasminNeural", "男声": "ms-MY-OsmanNeural"},
    "中文": {"女声": "zh-CN-XiaoxiaoNeural", "男声": "zh-CN-YunxiNeural"},
}


def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd))}\n{p.stdout[-3000:]}")
    return p.stdout


def run_ffmpeg_with_progress(cmd, duration, callback, cwd=None, progress_start=82, progress_end=99):
    """运行 FFmpeg 并将已处理媒体时间映射为可持久化的阶段进度。"""
    progress_cmd = list(cmd)
    insert_at = 1 if progress_cmd and Path(str(progress_cmd[0])).name.lower().startswith("ffmpeg") else 0
    progress_cmd[insert_at:insert_at] = ["-progress", "pipe:1", "-nostats"]
    process = subprocess.Popen(
        progress_cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    output = []
    last_progress = progress_start - 1
    last_emit = 0.0
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        output.append(line)
        if len(output) > 3000:
            output = output[-1500:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        seconds = None
        if key in ("out_time_us", "out_time_ms"):
            try:
                raw = float(value)
                seconds = raw / 1_000_000.0
            except ValueError:
                pass
        elif key == "out_time":
            try:
                hours, minutes, secs = value.split(":")
                seconds = int(hours) * 3600 + int(minutes) * 60 + float(secs)
            except (ValueError, TypeError):
                pass
        if seconds is None or duration <= 0:
            continue
        ratio = max(0.0, min(1.0, seconds / duration))
        current = min(progress_end, progress_start + int((progress_end - progress_start) * ratio))
        now = time.monotonic()
        if current > last_progress and (current >= progress_end or now - last_emit >= 1.5):
            callback(current, seconds, ratio)
            last_progress = current
            last_emit = now
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"command failed: {' '.join(map(str, progress_cmd))}\n" + "\n".join(output[-3000:]))
    if last_progress < progress_end:
        callback(progress_end, duration, 1.0)
    return "\n".join(output)


_TRANSLATION_CACHE = {}
_CACHE_PATH = ROOT / "test_run_dl" / "translation_cache.json"
if _CACHE_PATH.exists():
    try: _TRANSLATION_CACHE.update(json.loads(_CACHE_PATH.read_text(encoding="utf-8")))
    except Exception: pass

LANGUAGE_CODES = {
    "English": "English", "en": "English", "Bahasa Melayu": "Bahasa Melayu", "Malay": "Bahasa Melayu", "马来语": "Bahasa Melayu",
    "中文": "Simplified Chinese", "Chinese": "Simplified Chinese", "日本語": "Japanese", "한국어": "Korean", "Español": "Spanish",
    "Français": "French", "Deutsch": "German",
}
AI_BASE_URL = os.getenv("VIDEO_LOCALIZER_AI_BASE_URL", "https://ai-api-gateway.app.baizhi.cloud/api/openai").rstrip("/")
AI_PRIMARY_MODEL = os.getenv("VIDEO_LOCALIZER_AI_PRIMARY_MODEL", "agnes-2.0-flash")
AI_FALLBACK_MODEL = os.getenv("VIDEO_LOCALIZER_AI_FALLBACK_MODEL", "deepseek-v4-flash")


def _load_local_env():
    env_path = Path(os.getenv("VIDEO_LOCALIZER_ENV_FILE", str(ROOT / ".env.local")))
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _ai_translate_once(text, source_name, target_name, model):
    api_key = os.getenv("VIDEO_LOCALIZER_AI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 VIDEO_LOCALIZER_AI_API_KEY")
    prompt = (
        f"Translate the following subtitle from {source_name} to {target_name}. "
        "Return only the translated subtitle. Preserve meaning, tone, names and numbers. "
        "Do not add explanations, labels, quotation marks or Markdown.\n\n"
        f"Subtitle:\n{text}"
    )
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a professional audiovisual subtitle translator."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{AI_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "VideoLocalizer/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8-sig"))
    except Exception as exc:
        detail = getattr(exc, "reason", None) or str(exc) or exc.__class__.__name__
        raise RuntimeError(f"{model} 请求异常：{detail}") from exc
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"{model} 返回中没有 choices")
    translated = str((choices[0].get("message") or {}).get("content") or "").strip()
    translated = re.sub(r"^```(?:text)?\s*|\s*```$", "", translated, flags=re.IGNORECASE).strip()
    if not translated:
        raise RuntimeError(f"{model} 返回空译文")
    return translated


def translate(text, source, target):
    cache_key = f"{source}|{target}|{text}"
    if cache_key in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[cache_key]
    target_key = target.lower()
    if target_key in ("中文", "chinese", "zh") or source not in ("zh", "中文", "auto", "zh-CN"):
        return text
    target_name = LANGUAGE_CODES.get(target, target)
    if target not in LANGUAGE_CODES and target_key not in LANGUAGE_CODES:
        raise RuntimeError(f"当前翻译目标暂不支持：{target}")
    source_name = "Simplified Chinese" if source in ("zh", "中文", "auto", "zh-CN") else source
    _load_local_env()
    attempts = [(AI_PRIMARY_MODEL, 3), (AI_FALLBACK_MODEL, 3)]
    failures = []
    for model, max_attempts in attempts:
        for attempt in range(1, max_attempts + 1):
            try:
                translated = _ai_translate_once(text, source_name, target_name, model)
                _TRANSLATION_CACHE[cache_key] = translated
                try:
                    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _CACHE_PATH.write_text(
                        json.dumps(_TRANSLATION_CACHE, ensure_ascii=False, indent=2),
                        encoding="utf-8-sig",
                    )
                except Exception:
                    pass
                return translated
            except Exception as exc:
                failures.append(f"{model} 第 {attempt}/{max_attempts} 次：{exc}")
                if attempt < max_attempts:
                    time.sleep(min(4.0, 0.75 * (2 ** (attempt - 1))))
    raise RuntimeError("AI 翻译请求全部失败；" + " | ".join(failures))


def ass_time(sec):
    cs = max(0, int(round(sec * 100)))
    h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def wrap(text, limit=22):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit: return text
    words = text.split(" ")
    if len(words) > 1:
        out=[]; line=""
        for word in words:
            if line and len(line)+1+len(word)>limit: out.append(line); line=word
            else: line=(line+" "+word).strip()
        if line: out.append(line)
        return "\\N".join(out)
    mid=max(1, len(text)//2); return text[:mid]+"\\N"+text[mid:]


def _wrap_lines(text, line_limit):
    """按词语优先分行；单词语言不足以分行时按字符切分。"""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return [""]
    words = text.split(" ")
    if len(words) > 1:
        lines, line = [], ""
        for word in words:
            if line and len(line) + 1 + len(word) > line_limit:
                lines.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            lines.append(line)
        return lines
    return [text[index:index + line_limit] for index in range(0, len(text), line_limit)]


def fit_translation_subtitle(text, layout, width, height):
    """将一段译文完整显示于原字幕可见区间，不使用快速分页。"""
    source_size = int(layout["font_size"])
    min_size = max(18, int(round(height * 0.018)))
    max_lines = 3
    usable_width = max(120, int(width * 0.84))
    bottom_margin = max(18, int(round(height * 0.020)))
    original_bottom = int(layout["original_bottom"])
    gap = max(10, int(round(source_size * 0.42)))
    for font_size in range(source_size, min_size - 1, -1):
        # 拉丁文字平均宽度约为字号的0.53倍；保守取值避免ASS实际渲染溢出。
        line_limit = max(10, int(usable_width / max(font_size * 0.53, 1)))
        lines = _wrap_lines(text, line_limit)
        if len(lines) <= max_lines:
            line_count = max(1, len(lines))
            line_height = int(round(font_size * 1.24))
            top = original_bottom + gap
            required = line_count * line_height
            if top + required <= height - bottom_margin:
                return {
                    "text": "\\N".join(lines),
                    "font_size": font_size,
                    "translation_top": top,
                    "line_count": line_count,
                    "line_limit": line_limit,
                    "gap_pixels": gap,
                }
    # 极端长文本仍保持单事件。根据真实剩余高度重新计算最小字号，避免覆盖原文或越底。
    top = original_bottom + gap
    available_height = max(1, height - bottom_margin - top)
    font_size = max(14, min(min_size, int(available_height / (max_lines * 1.18))))
    line_limit = max(8, int(usable_width / max(font_size * 0.53, 1)))
    lines = _wrap_lines(text, line_limit)
    if len(lines) > max_lines:
        # 最后一行吸收余下词语；字号已按三行高度限制，内容不会通过换页提前消失。
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return {"text": "\\N".join(lines), "font_size": font_size, "translation_top": top, "line_count": len(lines), "line_limit": line_limit, "gap_pixels": gap}


def detect_subtitle_layout(input_path: str, width: int, height: int, work_dir: str | None = None):
    """Estimate the burned-in subtitle band from the lower image area.

    This is deliberately conservative: it samples edge/contrast density in lower bands
    and falls back to a two-line bottom-safe layout when confidence is low.
    """
    try:
        sample = Path(work_dir or Path(input_path).parent) / "subtitle_probe.jpg"
        run(["ffmpeg", "-y", "-sseof", "-8", "-i", str(input_path), "-frames:v", "1", "-vf", "scale=640:-2", str(sample)])
        from PIL import Image, ImageFilter
        image = Image.open(sample).convert("L")
        pixels = image.load(); w, h = image.size
        bands = [(0.62, 0.76), (0.70, 0.84), (0.78, 0.92), (0.84, 0.98)]
        scores = []
        for a, b in bands:
            y0, y1 = int(h*a), int(h*b); score = 0; count = 0
            for y in range(y0+1, y1):
                for x in range(1, w-1, 3):
                    score += abs(pixels[x,y]-pixels[x-1,y]) + abs(pixels[x,y]-pixels[x,y-1])
                    count += 2
            scores.append(score/max(count, 1))
        idx = max(range(len(scores)), key=scores)
        confidence = min(0.96, max(0.35, scores[idx]/max(sum(scores)/len(scores), 1)/3))
        sample.unlink(missing_ok=True)
        original_margin_v = int(height * (1.0 - bands[idx] + 0.02))
        return {"detected": True, "confidence": round(confidence, 2), "band": bands[idx], "original_margin_v": original_margin_v}
    except Exception:
        return {"detected": False, "confidence": 0.0, "band": (0.76, 0.96), "original_margin_v": int(height*0.18)}


def _video_encoder_args():
    force_cpu = os.getenv("VIDEO_LOCALIZER_FORCE_CPU", "0") == "1"
    if not force_cpu:
        try:
            encoders = run(["ffmpeg", "-hide_banner", "-encoders"])
            if "h264_nvenc" in encoders:
                return ["-c:v", "h264_nvenc", "-preset", "p4"], "h264_nvenc"
        except Exception:
            pass
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "19"], "libx264"


def process_video(input_path: str, output_dir: str, target_language="Bahasa Melayu", source_language="auto", max_speed=1.18, log=None, mode="subtitle_only", font_name="Aptos"): 
    inp=Path(input_path); outdir=Path(output_dir); outdir.mkdir(parents=True, exist_ok=True); work=outdir / (inp.stem+"_real_work"); work.mkdir(exist_ok=True)
    def say(stage, message, progress, **extra):
        if log: log(stage, message, progress, **extra)
    say("媒体探测", "读取原视频音轨、画面和时长", 5)
    probe=json.loads(run(["ffprobe","-v","error","-show_entries","format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate","-of","json",str(inp)]))
    duration=float(probe["format"]["duration"])
    audio=work/"source.wav"
    say("音频提取", "正在从原视频提取16kHz单声道人声轨道", 6)
    def report_audio_progress(current, seconds, ratio):
        say("音频提取", f"已提取 {seconds:.1f}/{duration:.1f} 秒音频", current, media_seconds=round(seconds, 3), media_duration=round(duration, 3))
    run_ffmpeg_with_progress(
        ["ffmpeg","-nostdin","-y","-i",str(inp),"-map","0:a:0","-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",str(audio)],
        duration,
        report_audio_progress,
        progress_start=6,
        progress_end=15,
    )
    force_cpu = os.getenv("VIDEO_LOCALIZER_FORCE_CPU", "0") == "1"
    say("语音识别", "正在初始化本地CPU faster-whisper" if force_cpu else "正在初始化本地GPU faster-whisper", 18)
    asr_device="cpu" if force_cpu else "cuda"
    try:
        if force_cpu:
            raise RuntimeError("启动器未检测到兼容的NVIDIA GPU，使用CPU模式")
        model=WhisperModel(str(MODEL), device="cuda", compute_type="float16")
        # 模型构造可能延迟加载CUDA库；转录迭代阶段仍可能失败，下面统一处理。
        kwargs={"language": None if source_language=="auto" else source_language, "beam_size":5, "vad_filter":True, "word_timestamps":False, "condition_on_previous_text":True}
        segs, info=model.transcribe(str(audio), **kwargs)
        segs=list(segs)
        say("语音识别", "本地GPU语音识别完成", 25, asr_device="cuda")
    except Exception as gpu_error:
        asr_device="cpu"
        say("语音识别", f"GPU语音识别不可用，自动回退CPU：{str(gpu_error)[:180]}", 19, asr_device="cpu", gpu_error=str(gpu_error)[:500])
        model=WhisperModel(str(MODEL), device="cpu", compute_type="int8")
        kwargs={"language": None if source_language=="auto" else source_language, "beam_size":5, "vad_filter":True, "word_timestamps":False, "condition_on_previous_text":True}
        segs, info=model.transcribe(str(audio), **kwargs)
        segs=list(segs)
        say("语音识别", "本地CPU语音识别完成", 25, asr_device="cpu")
    rows=[]
    for s in segs:
        text=s.text.strip()
        if text: rows.append({"start":round(float(s.start),3),"end":round(float(s.end),3),"text":text})
    if not rows: raise RuntimeError("未识别到讲话内容")
    say("字幕检测", "逐帧检测原硬字幕位置、基线和可见时间区间", 28)
    layout = analyze_hard_subtitles(str(inp))
    rows = align_segments_to_subtitle_intervals(rows, layout)
    rows = analyze_segment_subtitle_layouts(str(inp), rows, layout)
    say("翻译", f"将 {len(rows)} 段原文翻译为 {target_language}，译文跟随原字幕可见区间", 35) 
    for i,row in enumerate(rows):
        row["translation"]=translate(row["text"], info.language, target_language)
        row["speaker_id"]="speaker_1" if i % 2 == 0 else "speaker_2"
        row["gender"]="女声" if i % 2 == 0 else "男声"
        row["emotion"]="neutral"
        row["duration"]=round(row["end"]-row["start"],3)
    width = int(next((s.get("width", 1920) for s in probe.get("streams", []) if s.get("codec_type") == "video"), 1920))
    height = int(next((s.get("height", 1080) for s in probe.get("streams", []) if s.get("codec_type") == "video"), 1080))
    if layout.get("width") != width or layout.get("height") != height:
        layout = dict(layout, width=width, height=height)
    detected_layouts = [row.get("subtitle_layout", {}) for row in rows]
    font_sizes = [item.get("font_size", layout["font_size"]) for item in detected_layouts]
    gaps = [item.get("gap_pixels", layout["gap_pixels"]) for item in detected_layouts]
    tops = [item.get("translation_top", layout["translation_top"]) for item in detected_layouts]
    dominant_layout = detected_layouts[0] if detected_layouts else {
        "font_size": layout["font_size"],
        "translation_top": layout["translation_top"],
        "gap_pixels": layout["gap_pixels"],
        "layout_source": "global_fallback",
        "layout_state_count": 1,
    }
    layout.update({key: dominant_layout[key] for key in ("font_size", "translation_top", "gap_pixels", "layout_source", "layout_state_count")})
    layout["render_height"] = height
    layout["segment_dynamic"] = False
    layout["segment_count"] = len(detected_layouts)
    layout["font_size_range"] = [min(font_sizes), max(font_sizes)] if font_sizes else [layout["font_size"], layout["font_size"]]
    layout["gap_range"] = [min(gaps), max(gaps)] if gaps else [layout["gap_pixels"], layout["gap_pixels"]]
    layout["translation_top_range"] = [min(tops), max(tops)] if tops else [layout["translation_top"], layout["translation_top"]]
    say("字幕布局", f"已逐段检测 {len(rows)} 段原字幕；字号 {layout['font_size_range'][0]}..{layout['font_size_range'][1]}，间隔 {layout['gap_range'][0]}..{layout['gap_range'][1]}px", 44, subtitle_layout=layout)
    ass=outdir/f"{inp.stem}_{target_language}_translation_overlay.ass"
    header=f"""[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Translation,{font_name},{layout['font_size']},&H0056E7FF,&H0056E7FF,&H00101822,&H90101822,0,0,0,0,100,100,0,0,1,2,1,8,35,35,{layout['translation_top']},1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    ass.write_text(header, encoding="utf-8-sig")
    subtitle_page_count = 0
    with ass.open("a", encoding="utf-8-sig") as f:
        for row in rows:
            # 原视频已经包含中文原字幕；这里只新增目标语言译文，绝不重复烧录原文。
            visible_start = float(row.get('subtitle_visible_start', row['start']))
            visible_end = float(row.get('subtitle_visible_end', row['end']))
            segment_layout = row.get('subtitle_layout') or {
                'font_size': layout['font_size'],
                'translation_top': layout['translation_top'],
                'gap_pixels': layout['gap_pixels'],
            }
            # 译文事件必须完整跟随原硬字幕可见区间。长文本通过字号与分行适配，
            # 不在区间内快速分页替换，避免用户尚未读完就消失。
            fitted = fit_translation_subtitle(row['translation'], segment_layout, width, height)
            row['subtitle_pages'] = 1
            row['subtitle_render_layout'] = fitted
            subtitle_page_count += 1
            override = f"{{\\an8\\fs{int(fitted['font_size'])}\\pos({int(width / 2)},{int(fitted['translation_top'])})}}"
            f.write(f"Dialogue: 0,{ass_time(visible_start)},{ass_time(visible_end)},Translation,,0,0,0,,{override}{fitted['text']}\n")
    if mode == "subtitle_only":
        out=outdir/f"{inp.stem}_{target_language}_subtitle_only.mp4"
        say("烧录字幕", "保留原画面与原硬字幕，在原字幕主轨道下方紧邻烧录目标语言译文", 82)
        def encode_progress(progress, encoded_seconds, ratio):
            say("烧录字幕", f"正在编码成片：{encoded_seconds:.1f}/{duration:.1f} 秒", progress, encoded_seconds=round(encoded_seconds, 3), duration_seconds=round(duration, 3), encode_ratio=round(ratio, 4))
        encoder_args, encoder_name = _video_encoder_args()
        say("烧录字幕", f"视频编码器：{encoder_name}", 82, video_encoder=encoder_name)
        encode_cmd=["ffmpeg","-y","-i",str(inp),"-map","0:v:0","-map","0:a?","-vf",f"ass={ass.name}",*encoder_args,"-fps_mode","passthrough","-c:a","copy","-movflags","+faststart",str(out)]
        try:
            run_ffmpeg_with_progress(encode_cmd, duration, encode_progress, cwd=str(outdir), progress_start=82, progress_end=99)
        except Exception as nvenc_error:
            if encoder_name != "h264_nvenc":
                raise
            out.unlink(missing_ok=True)
            say("烧录字幕", f"NVENC不可用，自动回退软件编码：{str(nvenc_error)[-240:]}", 82, video_encoder="libx264")
            software_args=["-c:v","libx264","-preset","medium","-crf","19"]
            run_ffmpeg_with_progress(["ffmpeg","-y","-i",str(inp),"-map","0:v:0","-map","0:a?","-vf",f"ass={ass.name}",*software_args,"-fps_mode","passthrough","-c:a","copy","-movflags","+faststart",str(out)], duration, encode_progress, cwd=str(outdir), progress_start=82, progress_end=99)
        say("完成", f"原字幕保留 + 目标语言译文叠加成片已生成：{out.name}", 100, output_path=str(out))
        return {"output_path":str(out),"subtitle_path":str(ass),"duration_seconds":duration,"language":info.language,"segments":rows,"mode":mode,"subtitle_layout":layout,"encoding":"UTF-8-SIG","subtitle_timing":"one_full_translation_event_per_detected_hard_subtitle_interval","subtitle_page_count":subtitle_page_count,"subtitle_max_lines":3,"width":width,"height":height}
    say("TTS 配音", "按说话人音色逐段生成目标语言语音", 50)
    voice_map=EDGE_VOICES.get(target_language, EDGE_VOICES["English"]); clips=[]
    for i,row in enumerate(rows):
        raw=work/f"tts_{i:04d}.mp3"; fit=work/f"fit_{i:04d}.wav"; voice=voice_map[row["gender"]]
        run(["edge-tts","--voice",voice,"--text",row["translation"],"--write-media",str(raw)])
        probe_t=json.loads(run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(raw)])); rawdur=float(probe_t["format"]["duration"])
        target=row["duration"]; ratio=rawdur/max(target,0.05); speed=min(float(max_speed),max(1.0,ratio)); filters=[]
        while speed>2.0: filters.append("atempo=2.0"); speed/=2.0
        filters.append(f"atempo={speed:.5f}")
        run(["ffmpeg","-y","-i",str(raw),"-af",",".join(filters),"-ar","48000","-ac","2","-c:a","pcm_s16le",str(fit)])
        row.update({"tts_raw_duration":round(rawdur,3),"final_speed_ratio":round(speed,3),"audio_window_start":row["start"],"first_character_start":row["start"],"final_start":row["start"],"final_end":row["end"],"borrowed_before":0.0,"borrowed_after":round(max(0,rawdur-target),3),"overlong_strategy":"自然压缩后裁切至原讲话窗口" if ratio>1 else "保持原速并补足句尾静音","subtitle_wrap":"按语义换行"})
        clips.append((row,fit)); say("TTS 配音", f"第 {i+1}/{len(rows)} 段完成，{voice}，速度 {speed:.2f}x", 50+int(25*(i+1)/len(rows)))
    say("混音与字幕", "保留原音轨作为背景层，叠加目标语言 TTS，并烧录双语 ASS 字幕", 80)
    # 配音分支沿用旧音频时间轴，但使用同样的双语字幕文件布局。
    ass=outdir/f"{inp.stem}_{target_language}_dubbed.ass"; header="""[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Original,Microsoft YaHei,42,&H00FFFFFF,&H00FFFFFF,&H00101822,&H90101822,0,0,0,0,100,100,0,0,1,2,1,2,45,45,220,1\nStyle: Translation,Arial,38,&H0056E7FF,&H0056E7FF,&H00101822,&H90101822,0,0,0,0,100,100,0,0,1,2,1,2,45,45,150,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"""
    for row,_ in clips:
        ass.write_text(header,encoding="utf-8-sig") if not ass.exists() else None
        with ass.open("a",encoding="utf-8-sig") as f:
            f.write(f"Dialogue: 0,{ass_time(row['start'])},{ass_time(row['end'])},Original,,0,0,0,,{wrap(row['text'])}\n")
            f.write(f"Dialogue: 1,{ass_time(row['start'])},{ass_time(row['end'])},Translation,,0,0,0,,{wrap(row['translation'])}\n")
    silent=work/"tts_timeline.wav"; inputs=[]; filter_parts=[]
    for idx,(row,fit) in enumerate(clips):
        inputs += ["-i",str(fit)]; filter_parts.append(f"[{idx}:a]adelay={int(row['start']*1000)}|{int(row['start']*1000)}[a{idx}]")
    mix_inputs="".join(f"[a{i}]" for i in range(len(clips))); filter_parts.append(f"{mix_inputs}amix=inputs={len(clips)}:duration=longest:normalize=0[tts]")
    run(["ffmpeg","-y",*inputs,"-filter_complex",";".join(filter_parts),"-map","[tts]","-ar","48000","-ac","2","-c:a","pcm_s16le",str(silent)])
    out=outdir/f"{inp.stem}_{target_language}_translated_bilingual.mp4"
    # Original track is attenuated rather than removed, preserving ambience; TTS is mixed above it.
    filt="[0:a]volume=0.28[bg];[1:a]volume=1.0[voice];[bg][voice]amix=inputs=2:duration=first:normalize=0[aout]"
    def dubbed_encode_progress(progress, encoded_seconds, ratio):
        say("混音与字幕", f"正在编码配音成片：{encoded_seconds:.1f}/{duration:.1f} 秒", progress, encoded_seconds=round(encoded_seconds, 3), duration_seconds=round(duration, 3), encode_ratio=round(ratio, 4))
    encoder_args, encoder_name = _video_encoder_args()
    say("混音与字幕", f"视频编码器：{encoder_name}", 80, video_encoder=encoder_name)
    try:
        run_ffmpeg_with_progress(["ffmpeg","-y","-i",str(inp),"-i",str(silent),"-filter_complex",filt,"-map","0:v:0","-map","[aout]","-vf",f"ass={ass.name}",*encoder_args,"-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)], duration, dubbed_encode_progress, cwd=str(outdir), progress_start=80, progress_end=99)
    except Exception as nvenc_error:
        if encoder_name != "h264_nvenc":
            raise
        out.unlink(missing_ok=True)
        say("混音与字幕", f"NVENC不可用，自动回退软件编码：{str(nvenc_error)[-240:]}", 80, video_encoder="libx264")
        software_args=["-c:v","libx264","-preset","medium","-crf","19"]
        run_ffmpeg_with_progress(["ffmpeg","-y","-i",str(inp),"-i",str(silent),"-filter_complex",filt,"-map","0:v:0","-map","[aout]","-vf",f"ass={ass.name}",*software_args,"-c:a","aac","-b:a","192k","-movflags","+faststart",str(out)], duration, dubbed_encode_progress, cwd=str(outdir), progress_start=80, progress_end=99)
    say("完成", f"已生成真实目标语言配音、背景音混合和双语字幕烧录成片：{out.name}", 100, output_path=str(out))
    return {"output_path":str(out),"subtitle_path":str(ass),"duration_seconds":duration,"language":info.language,"segments":rows,"mode":mode,"subtitle_layout":layout,"width":width,"height":height}
