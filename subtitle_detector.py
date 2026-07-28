from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


def _subtitle_mask(frame: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    white = cv2.inRange(hsv, np.array([0, 0, 178], dtype=np.uint8), np.array([179, 88, 255], dtype=np.uint8))
    yellow = cv2.inRange(hsv, np.array([10, 82, 155], dtype=np.uint8), np.array([43, 255, 255], dtype=np.uint8))
    bright = cv2.bitwise_or(white, yellow)
    # 硬字幕通常有暗色描边：亮色字形周围必须能观察到暗像素，否则多为皮肤、衣服或产品高光。
    dark = cv2.inRange(gray, 0, 92)
    dark_near = cv2.dilate(dark, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
    mask = cv2.bitwise_and(bright, dark_near)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), dtype=np.uint8))
    return mask


def _frame_signature(frame: np.ndarray, y0: int, y1: int) -> np.ndarray:
    roi = frame[y0:y1]
    if roi.size == 0:
        return np.zeros((48, 120), dtype=np.uint8)
    mask = _subtitle_mask(roi)
    h, w = mask.shape
    mask[:, :int(w * 0.06)] = 0
    mask[:, int(w * 0.94):] = 0
    return cv2.resize(mask, (120, 48), interpolation=cv2.INTER_AREA)


def _signature_distance(left: np.ndarray, right: np.ndarray) -> float:
    left_bin = left >= 96
    right_bin = right >= 96
    union = np.logical_or(left_bin, right_bin).sum()
    if union == 0:
        return 0.0
    intersection = np.logical_and(left_bin, right_bin).sum()
    return 1.0 - float(intersection / union)


def _text_rows(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h, w = gray.shape
    y_start = int(h * 0.48)
    roi = gray[y_start:int(h * 0.96)]
    grad = cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3)
    edge = np.abs(grad)
    threshold = max(24.0, float(np.percentile(edge, 91)))
    binary = (edge >= threshold).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, w // 120), 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    row_density = np.mean(binary > 0, axis=1)
    return row_density, binary


def analyze_hard_subtitles(video_path: str, sample_fps: float = 4.0) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps if fps else 0.0
    step = max(1, int(round(fps / sample_fps)))
    row_votes = np.zeros(height, dtype=np.float64)
    samples: list[dict] = []
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step:
            index += 1
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        density, _ = _text_rows(gray)
        offset = int(height * 0.48)
        local_threshold = max(0.035, float(np.percentile(density, 82)))
        active = density >= local_threshold
        row_votes[offset:offset + len(active)] += active.astype(np.float64)
        samples.append({"time": index / fps, "frame": frame})
        index += 1
    cap.release()
    if not samples:
        raise RuntimeError("视频没有可分析画面")
    votes = row_votes / len(samples)
    candidates = np.where(votes >= max(0.08, float(np.percentile(votes[int(height * 0.48):int(height * 0.96)], 88))))[0]
    if len(candidates):
        groups = np.split(candidates, np.where(np.diff(candidates) > 3)[0] + 1)
        groups = [g for g in groups if len(g) >= max(2, height // 320)]
    else:
        groups = []
    if groups:
        scored = []
        for group in groups:
            center = float(np.mean(group))
            score = float(np.sum(votes[group])) * (1.2 if center > height * 0.58 else 0.7)
            scored.append((score, group))
        group = max(scored, key=lambda item: item[0])[1]
        top = max(0, int(group[0] - height * 0.012))
        bottom = min(height - 1, int(group[-1] + height * 0.018))
        confidence = min(0.99, float(np.mean(votes[group]) * 3.2))
    else:
        top, bottom, confidence = int(height * 0.70), int(height * 0.80), 0.0
    band_height = max(1, bottom - top)
    signatures = []
    for item in samples:
        sig = _frame_signature(item["frame"], max(0, top - band_height // 3), min(height, bottom + band_height // 3))
        signatures.append(sig)
    changes = []
    anchor = signatures[0]
    candidate_index = None
    stable_count = 0
    for idx in range(1, len(signatures)):
        diff = _signature_distance(anchor, signatures[idx])
        if diff >= 0.62:
            if candidate_index is None:
                candidate_index = idx
                stable_count = 1
            elif _signature_distance(signatures[candidate_index], signatures[idx]) <= 0.42:
                stable_count += 1
            else:
                candidate_index = idx
                stable_count = 1
            if stable_count >= 2:
                changes.append(round(samples[candidate_index]["time"], 3))
                anchor = signatures[idx]
                candidate_index = None
                stable_count = 0
        else:
            candidate_index = None
            stable_count = 0
    boundaries = [0.0, *changes, round(duration, 3)]
    merged = [boundaries[0]]
    for value in boundaries[1:]:
        if value - merged[-1] >= 0.75:
            merged.append(value)
    if duration - merged[-1] >= 0.2:
        merged.append(round(duration, 3))
    translation_top = min(height - 1, bottom + max(6, int(height * 0.008)))
    font_size = max(24, min(44, int(height * 0.026)))
    return {
        "detected": bool(groups),
        "confidence": round(confidence, 3),
        "width": width,
        "height": height,
        "fps": round(fps, 6),
        "duration": round(duration, 3),
        "original_top": top,
        "original_bottom": bottom,
        "original_top_ratio": round(top / height, 4),
        "original_bottom_ratio": round(bottom / height, 4),
        "translation_top": translation_top,
        "gap_pixels": translation_top - bottom,
        "font_size": font_size,
        "change_boundaries": merged,
        "sample_count": len(samples),
    }


def _contiguous_groups(indices: np.ndarray, max_gap: int = 2) -> list[np.ndarray]:
    if len(indices) == 0:
        return []
    return list(np.split(indices, np.where(np.diff(indices) > max_gap)[0] + 1))


def _measure_persistent_subtitle_box(frames: list[np.ndarray], global_layout: dict) -> dict | None:
    """用多帧多数投票提取持续存在的硬字幕字形，抑制运动画面和瞬时高光。"""
    if len(frames) < 3:
        return None
    height, width = frames[0].shape[:2]
    global_top = int(global_layout.get("original_top", height * 0.68))
    global_bottom = int(global_layout.get("original_bottom", height * 0.82))
    pad = max(48, int(height * 0.12))
    y0 = max(int(height * 0.42), global_top - pad)
    y1 = min(int(height * 0.96), global_bottom + pad)
    x0, x1 = int(width * 0.06), int(width * 0.94)
    masks = []
    for frame in frames:
        roi = frame[y0:y1, x0:x1]
        mask = _subtitle_mask(roi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        masks.append(mask > 0)
    votes = np.sum(np.stack(masks, axis=0), axis=0)
    persistent = (votes >= max(3, int(np.ceil(len(frames) * 0.60)))).astype(np.uint8) * 255
    persistent = cv2.morphologyEx(
        persistent,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, width // 180), 2)),
    )
    contours, _ = cv2.findContours(persistent, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    glyphs = []
    min_h = max(6, int(height * 0.008))
    max_h = max(40, int(height * 0.060))
    for contour in contours:
        x, y, glyph_w, glyph_h = cv2.boundingRect(contour)
        if min_h <= glyph_h <= max_h and 2 <= glyph_w <= int(width * 0.16):
            glyphs.append((x, y, glyph_w, glyph_h))
    if len(glyphs) < 3:
        return None

    baselines = np.array([y + glyph_h for x, y, glyph_w, glyph_h in glyphs], dtype=np.int32)
    order = np.argsort(baselines)
    groups: list[list[tuple[int, int, int, int]]] = []
    tolerance = max(8, int(height * 0.012))
    for idx in order:
        glyph = glyphs[int(idx)]
        baseline = glyph[1] + glyph[3]
        if not groups:
            groups.append([glyph])
            continue
        center = float(np.median([item[1] + item[3] for item in groups[-1]]))
        if abs(baseline - center) <= tolerance:
            groups[-1].append(glyph)
        else:
            groups.append([glyph])

    candidates = []
    global_center = (global_top + global_bottom) / 2
    for group in groups:
        left = min(item[0] for item in group)
        right = max(item[0] + item[2] for item in group)
        top = min(item[1] for item in group)
        bottom = max(item[1] + item[3] for item in group)
        span = right - left
        center_x = (left + right) / 2
        absolute_center_y = y0 + (top + bottom) / 2
        if len(group) < 3 or span < width * 0.10:
            continue
        if abs((x0 + center_x) - width / 2) > width * 0.28:
            continue
        proximity = max(0.0, 1.0 - abs(absolute_center_y - global_center) / max(height * 0.20, 1))
        coverage = min(1.0, span / max(width * 0.42, 1))
        support = min(1.0, len(group) / 10.0)
        candidates.append((coverage * 0.45 + support * 0.35 + proximity * 0.20, group))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = [candidates[0][1]]
    first_bottom = max(item[1] + item[3] for item in selected[0])
    for _, group in candidates[1:]:
        group_bottom = max(item[1] + item[3] for item in group)
        if abs(group_bottom - first_bottom) <= max(52, int(height * 0.045)):
            selected.append(group)
            if len(selected) >= 2:
                break
    all_glyphs = [item for group in selected for item in group]
    left = x0 + min(item[0] for item in all_glyphs)
    right = x0 + max(item[0] + item[2] for item in all_glyphs)
    top = y0 + min(item[1] for item in all_glyphs)
    bottom = y0 + max(item[1] + item[3] for item in all_glyphs)
    glyph_heights = [item[3] for item in all_glyphs]
    line_height = float(np.median(glyph_heights))
    global_font = int(global_layout.get("font_size", max(24, height * 0.026)))
    font_size = max(int(global_font * 0.72), min(int(global_font * 1.35), int(round(line_height * 1.16))))
    return {
        "original_left": int(left),
        "original_right": int(right),
        "original_top": int(top),
        "original_bottom": int(bottom),
        "original_line_count": len(selected),
        "original_line_height": round(line_height, 2),
        "font_size": int(font_size),
        "confidence": round(float(min(0.99, candidates[0][0] + 0.18)), 3),
        "measurement_source": "persistent_multiframe_mask",
    }


def _measure_subtitle_box(frame: np.ndarray, global_layout: dict) -> dict | None:
    """在单帧中测量原硬字幕的包围框、行高和置信度。"""
    height, width = frame.shape[:2]
    global_top = int(global_layout.get("original_top", height * 0.68))
    global_bottom = int(global_layout.get("original_bottom", height * 0.82))
    pad = max(24, int(height * 0.055))
    y0 = max(int(height * 0.42), global_top - pad)
    y1 = min(int(height * 0.97), global_bottom + pad)
    x0, x1 = int(width * 0.04), int(width * 0.96)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    mask = _subtitle_mask(roi)
    # 字幕字形通常由许多相近高度的小连通域组成；先去除孤立高光，再轻微横向连接字形。
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    joined = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, width // 180), 2)),
    )
    row_density = np.mean(joined > 0, axis=1)
    row_threshold = max(0.012, float(np.percentile(row_density, 82)) * 0.48)
    active_rows = np.where(row_density >= row_threshold)[0]
    groups = [g for g in _contiguous_groups(active_rows, max_gap=max(2, height // 360)) if len(g) >= max(3, height // 320)]
    if not groups:
        return None
    # 优先选择靠近全局字幕带且具有足够横向字符覆盖的最多两行文本。
    candidates = []
    global_center = (global_top + global_bottom) / 2 - y0
    global_band_height = max(12, global_bottom - global_top)
    for group in groups:
        group_height = int(group[-1] - group[0] + 1)
        # 单行字幕带不应覆盖画面的大块区域；过高区域通常是人物衣服、特效或场景高光。
        if group_height > max(int(height * 0.075), int(global_band_height * 1.35)):
            continue
        group_mask = joined[group[0]:group[-1] + 1]
        cols = np.where(np.mean(group_mask > 0, axis=0) >= 0.025)[0]
        if len(cols) < max(8, width // 80):
            continue
        center = float(np.mean(group))
        proximity = max(0.0, 1.0 - abs(center - global_center) / max(pad * 1.45, 1))
        if proximity < 0.14:
            continue
        coverage = min(1.0, len(cols) / max(width * 0.22, 1))
        density = min(1.0, float(np.mean(row_density[group])) / 0.12)
        score = proximity * 0.58 + coverage * 0.27 + density * 0.15
        candidates.append((score, group, cols))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = [candidates[0]]
    for item in candidates[1:]:
        if len(selected) >= 2:
            break
        gap = min(abs(int(item[1][0]) - int(selected[0][1][-1])), abs(int(selected[0][1][0]) - int(item[1][-1])))
        if gap <= max(18, height // 28):
            selected.append(item)
    top_local = min(int(item[1][0]) for item in selected)
    bottom_local = max(int(item[1][-1]) for item in selected)
    selected_mask = joined[top_local:bottom_local + 1]
    col_density = np.mean(selected_mask > 0, axis=0)
    active_cols = np.where(col_density >= 0.018)[0]
    if len(active_cols) == 0:
        return None
    left = x0 + int(np.percentile(active_cols, 1))
    right = x0 + int(np.percentile(active_cols, 99))
    top = y0 + top_local
    bottom = y0 + bottom_local
    line_heights = [max(1, int(item[1][-1] - item[1][0] + 1)) for item in selected]
    # 用字形连通域高度而不是整行包围框估算字号，避免两行粘连或背景高光把字号放大。
    contour_source = mask[top_local:bottom_local + 1]
    contours, _ = cv2.findContours(contour_source, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    glyph_heights = []
    for contour in contours:
        _, _, glyph_w, glyph_h = cv2.boundingRect(contour)
        if max(3, int(height * 0.008)) <= glyph_h <= max(14, int(height * 0.065)) and glyph_w >= 2:
            glyph_heights.append(glyph_h)
    median_line_height = float(np.median(glyph_heights)) if len(glyph_heights) >= 4 else float(np.median(line_heights))
    # 常见黑体 ASS 字号约为可见字形高度的 1.10–1.22 倍。
    font_size = int(round(median_line_height * 1.16))
    global_font = int(global_layout.get("font_size", max(24, height * 0.026)))
    font_size = max(int(global_font * 0.72), min(int(global_font * 1.42), font_size))
    confidence = min(0.99, max(item[0] for item in selected))
    return {
        "original_left": left,
        "original_right": right,
        "original_top": top,
        "original_bottom": bottom,
        "original_line_count": len(selected),
        "original_line_height": round(median_line_height, 2),
        "font_size": font_size,
        "confidence": round(float(confidence), 3),
    }


def _median_layout(items: list[dict], height: int, global_layout: dict) -> dict:
    """把同一字幕轨道的多次观测压缩成一个鲁棒布局状态。"""
    global_font = int(global_layout.get("font_size", max(24, height * 0.026)))
    if not items:
        return {
            "original_left": int(global_layout.get("original_left", global_layout.get("width", height) * 0.1)),
            "original_right": int(global_layout.get("original_right", global_layout.get("width", height) * 0.9)),
            "original_top": int(global_layout["original_top"]),
            "original_bottom": int(global_layout["original_bottom"]),
            "original_line_count": 1,
            "original_line_height": round(float(global_layout["original_bottom"] - global_layout["original_top"]), 2),
            "font_size": global_font,
            "confidence": 0.0,
        }
    result = {
        key: int(round(float(np.median([item[key] for item in items]))))
        for key in ("original_left", "original_right", "original_top", "original_bottom", "original_line_count", "font_size")
    }
    result["original_line_height"] = round(float(np.median([item["original_line_height"] for item in items])), 2)
    result["confidence"] = round(float(np.median([item.get("confidence", 0.0) for item in items])), 3)
    return result


def _same_layout_track(left: dict, right: dict, height: int) -> bool:
    """字幕文本宽度允许剧烈变化，但基线、字高和行数必须属于同一轨道。"""
    bottom_limit = max(10, int(height * 0.018))
    top_limit = max(14, int(height * 0.024))
    font_limit = max(5, int(max(left["font_size"], right["font_size"]) * 0.24))
    return (
        abs(int(left["original_bottom"]) - int(right["original_bottom"])) <= bottom_limit
        and abs(int(left["original_top"]) - int(right["original_top"])) <= top_limit
        and abs(int(left["font_size"]) - int(right["font_size"])) <= font_limit
        and abs(int(left["original_line_count"]) - int(right["original_line_count"])) <= 1
    )


def _build_layout_states(measurements: list[dict | None], rows: list[dict], height: int, global_layout: dict) -> tuple[list[dict], list[int]]:
    """建立跨字幕段布局状态；单个误检不能创建新状态。"""
    reliable = [item for item in measurements if item and item.get("confidence", 0.0) >= 0.28]
    if not reliable:
        return [_median_layout([], height, global_layout)], [0] * len(rows)

    clusters: list[list[dict]] = []
    for item in reliable:
        matched = None
        best_distance = float("inf")
        for index, cluster in enumerate(clusters):
            center = _median_layout(cluster, height, global_layout)
            if _same_layout_track(center, item, height):
                distance = abs(center["original_bottom"] - item["original_bottom"]) + abs(center["font_size"] - item["font_size"]) * 2
                if distance < best_distance:
                    matched, best_distance = index, distance
        if matched is None:
            clusters.append([item])
        else:
            clusters[matched].append(item)

    # 极少出现的簇通常来自人物高光或画面文字；将其吸收到最近的主轨道。
    minimum_support = max(2, int(round(len(reliable) * 0.12)))
    major = [cluster for cluster in clusters if len(cluster) >= minimum_support]
    if not major:
        major = [max(clusters, key=len)]
    states = [_median_layout(cluster, height, global_layout) for cluster in major]

    assignments: list[int] = []
    previous = 0
    pending = None
    pending_count = 0
    for item in measurements:
        if not item or item.get("confidence", 0.0) < 0.28:
            assignments.append(previous)
            pending = None
            pending_count = 0
            continue
        distances = [
            abs(state["original_bottom"] - item["original_bottom"])
            + abs(state["original_top"] - item["original_top"]) * 0.5
            + abs(state["font_size"] - item["font_size"]) * 2
            for state in states
        ]
        candidate = int(np.argmin(distances))
        if candidate == previous:
            pending = None
            pending_count = 0
        elif pending == candidate:
            pending_count += 1
            # 至少两个连续字幕段支持新轨道才切换，防止单段乱飞。
            if pending_count >= 2:
                previous = candidate
                pending = None
                pending_count = 0
        else:
            pending = candidate
            pending_count = 1
        assignments.append(previous)

    # 前向确认会让切换点晚一段；确认后回填连续候选段，保持章节切换准确。
    for index in range(1, len(assignments)):
        if assignments[index] != assignments[index - 1]:
            assignments[index - 1] = assignments[index]
    return states, assignments


def analyze_segment_subtitle_layouts(video_path: str, rows: list[dict], global_layout: dict) -> list[dict]:
    """从整条视频的可靠测量中拟合一条主字幕轨道，译文固定紧邻其下方。"""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or global_layout.get("height") or 1080)
    measurements: list[dict | None] = []
    for row in rows:
        start = float(row.get("subtitle_visible_start", row["start"]))
        end = float(row.get("subtitle_visible_end", row["end"]))
        span = max(0.01, end - start)
        # 五帧采样比原三帧更能抵抗淡入、转场和单帧高光。
        times = [start + span * ratio for ratio in (0.18, 0.34, 0.50, 0.66, 0.82)]
        frames = []
        boxes = []
        for timestamp in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
                box = _measure_subtitle_box(frame, global_layout)
                if box:
                    boxes.append(box)
        persistent = _measure_persistent_subtitle_box(frames, global_layout)
        measurements.append(persistent or (_median_layout(boxes, height, global_layout) if len(boxes) >= 3 else None))
    cap.release()

    reliable = [item for item in measurements if item and float(item.get("confidence", 0.0)) >= 0.28]
    fallback = _median_layout(reliable, height, global_layout) if reliable else _median_layout([], height, global_layout)
    baseline_bottom = int(round(float(np.median([item["original_bottom"] for item in reliable])))) if reliable else int(fallback["original_bottom"])
    plausible_delta = max(70, int(height * 0.075))
    smoothed_bottoms: list[int] = []
    for item in measurements:
        # 高置信度测量可反映源硬字幕真实的上下换轨（例如人物对白与说明文字位置不同）。
        # 超出主轨道合理范围的孤立值才视为人物/产品高光误检并回退，不能把真实下移强行抹平。
        if (
            item
            and float(item.get("confidence", 0.0)) >= 0.90
            and abs(int(item["original_bottom"]) - baseline_bottom) <= plausible_delta
        ):
            # 主轨道底边是最低保护线。第二行只有一两个字时可能被连通域过滤掉，
            # 这类漏检不能让译文上移；可靠检测只允许把底边向下扩展。
            smoothed_bottoms.append(max(baseline_bottom, int(item["original_bottom"])))
        else:
            smoothed_bottoms.append(baseline_bottom)

    bottom_margin = max(18, int(round(height * 0.020)))
    for index, row in enumerate(rows):
        measurement = measurements[index]
        # 保留可靠测量的真实字形尺寸和最底行。旧版固定26px并只用平滑基线，
        # 双行原字幕会被当成单行，导致译文压到原文第二行。
        source = measurement if measurement and float(measurement.get("confidence", 0.0)) >= 0.28 else fallback
        font_size = int(source.get("font_size", fallback["font_size"]))
        font_size = max(int(height * 0.020), min(int(height * 0.050), font_size))
        gap = max(10, int(round(font_size * 0.42)))
        original_bottom = int(smoothed_bottoms[index])
        translation_top = original_bottom + gap
        available = height - bottom_margin - translation_top
        if available < font_size * 2.45:
            font_size = max(int(height * 0.018), int(available / 2.45))
        translation_top = min(translation_top, height - bottom_margin - int(font_size * 2.45))
        row["subtitle_layout"] = {
            **fallback,
            "original_left": int(source.get("original_left", fallback["original_left"])),
            "original_right": int(source.get("original_right", fallback["original_right"])),
            "original_top": int(source.get("original_top", fallback["original_top"])),
            "original_bottom": original_bottom,
            "original_line_count": int(source.get("original_line_count", fallback["original_line_count"])),
            "original_line_height": float(source.get("original_line_height", fallback["original_line_height"])),
            "font_size": int(font_size),
            "translation_top": int(translation_top),
            "gap_pixels": int(max(2, translation_top - original_bottom)),
            "layout_source": "persistent_track_constrained" if measurement else "persistent_track_interpolated",
            "layout_state_id": 0,
            "layout_state_count": 1,
            "render_height": height,
            "confidence": round(float(measurement.get("confidence", 0.0)) if measurement else 0.0, 3),
            "raw_original_bottom": int(measurement["original_bottom"]) if measurement else None,
            "raw_font_size": int(measurement["font_size"]) if measurement else None,
        }
    return rows


def align_segments_to_subtitle_intervals(rows: list[dict], layout: dict) -> list[dict]:
    boundaries = layout.get("change_boundaries") or []
    duration = float(layout.get("duration") or 0.0)
    if len(boundaries) < 2:
        return rows
    intervals = [(float(boundaries[i]), float(boundaries[i + 1])) for i in range(len(boundaries) - 1)]
    aligned = []
    for row in rows:
        start = float(row["start"])
        end = float(row["end"])
        midpoint = (start + end) / 2
        overlaps = [item for item in intervals if min(end, item[1]) - max(start, item[0]) > 0.05]
        if overlaps:
            chosen = max(overlaps, key=lambda item: min(end, item[1]) - max(start, item[0]))
        else:
            chosen = min(intervals, key=lambda item: abs(((item[0] + item[1]) / 2) - midpoint))
        visible_start = max(0.0, chosen[0])
        visible_end = min(duration, chosen[1])
        if visible_end - visible_start < 0.8:
            visible_end = min(duration, visible_start + max(0.8, end - start))
        new_row = dict(row)
        new_row.update({
            "asr_start": round(start, 3),
            "asr_end": round(end, 3),
            "subtitle_visible_start": round(visible_start, 3),
            "subtitle_visible_end": round(visible_end, 3),
            "subtitle_visible_duration": round(visible_end - visible_start, 3),
            "start": round(visible_start, 3),
            "end": round(visible_end, 3),
        })
        aligned.append(new_row)
    merged = []
    for row in aligned:
        if merged and abs(row["start"] - merged[-1]["start"]) < 0.08 and abs(row["end"] - merged[-1]["end"]) < 0.08:
            merged[-1]["text"] = (merged[-1]["text"] + " " + row["text"]).strip()
        else:
            merged.append(row)
    return merged


def save_analysis(path: str, result: dict) -> None:
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
