"""
Обработка видео через ffmpeg. Не зависит от Telegram — можно тестировать отдельно.

Для каждой копии генерируются случайные параметры: небольшое кадрирование,
цветокоррекция, скорость, зерно, громкость, а также другой битрейт/CRF.
Метаданные исходного файла всегда удаляются.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("refreshclip.processor")

# Множитель силы изменений для уровней: 1 — мягко, 2 — стандарт, 3 — сильно
LEVEL_FACTOR = {1: 0.5, 2: 1.0, 3: 1.6}

ProgressCallback = Callable[[int], Awaitable[None]]


class ProcessingError(Exception):
    """Ошибка обработки с готовым текстом для пользователя."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class Options:
    level: int = 2
    color: bool = True
    crop: bool = True
    speed: bool = True
    audio: bool = True
    noise: bool = False
    flip: bool = False


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass
class Plan:
    """Конкретные параметры одной копии."""

    vf: list[str]
    af: list[str]
    speed: float
    crf: int
    audio_bitrate: str
    summary: str


# ───────────────────────── ffprobe ─────────────────────────
async def probe(path: Path) -> VideoInfo:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProcessingError("⚠️ <b>Не удалось прочитать файл.</b>\nПопробуйте другое видео.")
    if proc.returncode != 0:
        raise ProcessingError("⚠️ <b>Не удалось прочитать файл.</b>\nПохоже, это не видео.")

    data = json.loads(out or b"{}")
    streams = data.get("streams", [])
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise ProcessingError("⚠️ <b>В файле нет видеодорожки.</b>")

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    try:
        rotation = int(float(video.get("tags", {}).get("rotate", 0)))
        for sd in video.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(float(sd["rotation"]))
    except (TypeError, ValueError):
        rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width

    # Неквадратные пиксели: приводим ширину к отображаемой
    try:
        sar_n, sar_d = (float(x) for x in str(video.get("sample_aspect_ratio", "1:1")).split(":"))
        if sar_n > 0 and sar_d > 0 and abs(sar_n / sar_d - 1) > 0.01:
            width = round(width * sar_n / sar_d)
    except (TypeError, ValueError):
        pass

    duration = 0.0
    for raw in (data.get("format", {}).get("duration"), video.get("duration")):
        try:
            duration = float(raw)
            break
        except (TypeError, ValueError):
            continue

    return VideoInfo(
        duration=duration,
        width=width,
        height=height,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


# ───────────────────────── План копии ─────────────────────────
def target_size(info: VideoInfo, max_side: int) -> tuple[int, int] | None:
    """Итоговый размер кадра (чётный, не больше max_side по длинной стороне)."""
    w, h = info.width, info.height
    if w <= 0 or h <= 0:
        return None
    longest = max(w, h)
    if longest > max_side:
        w, h = w * max_side / longest, h * max_side / longest
    return max(2, int(w) // 2 * 2), max(2, int(h) // 2 * 2)


def make_plan(opts: Options, info: VideoInfo, seed: int, *, extra_crf: int = 0, max_side: int = 1920) -> Plan:
    rng = random.Random(seed)
    k = LEVEL_FACTOR.get(opts.level, 1.0)

    # Ограничение размера, чётные размеры и квадратные пиксели
    size = target_size(info, max_side)
    if size:
        vf = [f"scale={size[0]}:{size[1]}", "setsar=1"]
    else:
        vf = [f"scale=w='if(gt(iw,ih),min(iw,{max_side}),-2)':h='if(gt(iw,ih),-2,min(ih,{max_side}))'", "setsar=1"]
    af: list[str] = []
    applied: list[str] = []
    speed = 1.0

    if opts.crop:
        cut = rng.uniform(0.012, 0.03) * k  # доля, срезаемая с каждой стороны
        f = 1 - 2 * cut
        px, py = rng.uniform(0.3, 0.7), rng.uniform(0.3, 0.7)
        vf.append(f"crop=iw*{f:.4f}:ih*{f:.4f}:(iw-ow)*{px:.3f}:(ih-oh)*{py:.3f}")
        if size:  # возвращаем исходный размер кадра
            vf.append(f"scale={size[0]}:{size[1]}:flags=lanczos")
        else:
            vf.append(f"scale=trunc(iw/{f:.4f}/2)*2:trunc(ih/{f:.4f}/2)*2:flags=lanczos")
        vf.append("setsar=1")
        applied.append("✂️ кадр")

    if opts.flip:
        vf.append("hflip")
        applied.append("🪞 зеркало")

    if opts.speed:
        sign = rng.choice((-1, 1))
        speed = 1 + sign * rng.uniform(0.012, 0.035) * k
        vf.append(f"setpts=PTS/{speed:.4f}")
        af.append(f"atempo={speed:.4f}")
        applied.append("⏩ скорость")

    if opts.color:
        b = rng.uniform(-0.03, 0.03) * k
        c = 1 + rng.uniform(-0.04, 0.04) * k
        s = 1 + rng.uniform(-0.08, 0.08) * k
        g = 1 + rng.uniform(-0.05, 0.05) * k
        h = rng.uniform(-3, 3) * k
        vf.append(f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}:gamma={g:.3f}")
        vf.append(f"hue=h={h:.2f}")
        applied.append("🎨 цвет")

    if opts.noise:
        vf.append(f"noise=alls={max(1, round(rng.uniform(2, 4) * k))}:allf=t")
        applied.append("🎞 зерно")

    vf.append("format=yuv420p")

    if opts.audio and info.has_audio:
        af.append(f"volume={rng.uniform(-1.5, 1.5) * k:.2f}dB")
        applied.append("🔊 звук")

    applied.append("🧹 метаданные")
    return Plan(
        vf=vf,
        af=af if info.has_audio else [],
        speed=speed,
        crf=rng.randint(21, 24) + extra_crf,
        audio_bitrate=rng.choice(("128k", "144k", "160k")),
        summary=" · ".join(applied),
    )


# ───────────────────────── Запуск ffmpeg ─────────────────────────
async def _run_ffmpeg(
    src: Path, dst: Path, plan: Plan, info: VideoInfo,
    *, threads: int, timeout: int, on_progress: ProgressCallback | None,
) -> None:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", ",".join(plan.vf),
    ]
    if plan.af and info.has_audio:
        cmd += ["-af", ",".join(plan.af)]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(plan.crf),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", plan.audio_bitrate, "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-threads", str(threads), "-max_muxing_queue_size", "1024",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stderr_tail = b""
    total = max(info.duration / plan.speed, 0.1)

    async def read_progress() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore").strip()
            if on_progress and line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    micro = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                await on_progress(max(0, min(99, int(micro / 1_000_000 / total * 100))))

    async def read_errors() -> None:
        nonlocal stderr_tail
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(4096):
            stderr_tail = (stderr_tail + chunk)[-4000:]

    try:
        await asyncio.wait_for(asyncio.gather(read_progress(), read_errors(), proc.wait()), timeout)
    except asyncio.TimeoutError:
        raise ProcessingError("⏱ <b>Обработка заняла слишком много времени.</b>\nПопробуйте видео покороче.")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    if proc.returncode != 0:
        log.error("ffmpeg failed (%s): %s", proc.returncode, stderr_tail.decode(errors="ignore"))
        raise ProcessingError("😔 <b>Не удалось обработать видео.</b>\nВозможно, формат файла не поддерживается.")


async def uniqualize(
    src: Path, dst: Path, info: VideoInfo, opts: Options,
    *, max_bytes: int, max_side: int = 1920, threads: int = 2, timeout: int = 900,
    on_progress: ProgressCallback | None = None,
) -> Plan:
    """Создаёт одну копию. Если файл не влезает в max_bytes — пересобирает с большим CRF."""
    seed = secrets.randbits(64)
    attempts = ((0, max_side), (6, min(max_side, 1280)), (10, min(max_side, 960)))
    for extra_crf, side in attempts:
        plan = make_plan(opts, info, seed, extra_crf=extra_crf, max_side=side)
        await _run_ffmpeg(src, dst, plan, info, threads=threads, timeout=timeout, on_progress=on_progress)
        if dst.stat().st_size <= max_bytes:
            return plan
        log.info("Результат %.1f МБ > лимита, пересобираю с CRF+%s", dst.stat().st_size / 1048576, extra_crf + 4)
    raise ProcessingError(
        "📦 <b>Результат получился слишком большим</b> для отправки в Telegram (лимит — 50 МБ).\n"
        "Попробуйте видео покороче."
    )
