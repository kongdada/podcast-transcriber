#!/usr/bin/env python3
"""Podcast download + transcription workflow.

Pipeline:
1. Resolve Apple Podcasts / Xiaoyuzhou URL with yt-dlp.
2. Download selected episode audio.
3. Transcode to MP3 with ffmpeg (only MP3 is retained).
4. Transcribe with local whisper.cpp model.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_ASR_MODEL = "./models/ggml-large-v3-turbo.bin"
DEFAULT_TDRZ_MODEL = "./models/ggml-small.en-tdrz.bin"
DEFAULT_WHISPER_BIN = "./build/bin/whisper-cli"
DEFAULT_PROFILE_DIR = "./scripts/podcast_profiles"
DEFAULT_LANGUAGE = "zh"
DEFAULT_THREADS = 8
DEFAULT_PROGRESS_INTERVAL_S = 30

INSTALL_HINTS = {
    "yt-dlp": ["brew install yt-dlp", "python3 -m pip install -U yt-dlp"],
    "ffmpeg": ["brew install ffmpeg"],
}


class WorkflowError(RuntimeError):
    """Workflow-level expected failure."""


@dataclass
class EpisodeCandidate:
    title: str
    source_url: str
    playlist_index: int | None
    release_ts: int
    duration_s: int | None
    uploader: str | None


@dataclass
class Segment:
    t0_ms: int
    t1_ms: int
    text: str
    speaker: str | None = None
    speaker_turn_next: bool = False


@dataclass
class SpeakerTurn:
    t0_ms: int
    t1_ms: int
    speaker: str | None
    parts: list[str]


@dataclass
class TranscriptionResult:
    command: list[str]
    transcript_json: dict[str, Any]


@dataclass
class DiarizationAssessment:
    text_similarity_ratio: float | None
    temporal_coverage_ratio: float | None
    speaker_turn_markers: int
    labeled_segment_ratio: float
    second_full_asr_recommended: bool
    note: str


@dataclass
class PodcastProfile:
    name: str
    path: Path
    source_url_regex: str | None
    title_regex: str | None
    input_url_regex: str | None
    speaker_a_name: str | None
    speaker_b_name: str | None
    noise_phrases: list[str]
    replacements: dict[str, str]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-shot local podcast download + transcription workflow")
    parser.add_argument("--url", required=True, help="Podcast episode/show URL (Apple Podcasts or Xiaoyuzhou)")
    parser.add_argument(
        "--episode-index",
        type=int,
        default=None,
        help="Select episode index (1-based) from recent-10 list when URL is a show page",
    )
    parser.add_argument("--out-root", default="./outputs", help="Output root directory")
    parser.add_argument("--profile", default=None, help="Optional profile name or JSON file path")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="Profile directory")
    parser.add_argument("--whisper-bin", default=DEFAULT_WHISPER_BIN, help="Path or name of whisper-cli")
    parser.add_argument("--asr-model", default=DEFAULT_ASR_MODEL, help="Path to ASR model")
    parser.add_argument(
        "--diarization",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable speaker diarization by tinydiarize model",
    )
    parser.add_argument("--speaker-a-name", default=None, help="Optional display name for Speaker A")
    parser.add_argument("--speaker-b-name", default=None, help="Optional display name for Speaker B")
    parser.add_argument("--tdrz-model", default=DEFAULT_TDRZ_MODEL, help="Path to tinydiarize model")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help="Whisper language code (default: zh)")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS, help="Whisper thread count")
    parser.add_argument(
        "--gpu",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable GPU acceleration when available",
    )
    parser.add_argument(
        "--keep-awake",
        action=argparse.BooleanOptionalAction,
        default=(sys.platform == "darwin"),
        help="Prevent system sleep during long ASR runs (macOS uses caffeinate)",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=DEFAULT_PROGRESS_INTERVAL_S,
        help="Progress heartbeat interval seconds (default: 30)",
    )
    parser.add_argument(
        "--keep-json-artifacts",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Keep optional debug artifacts such as run_manifest.json (JSON transcript artifacts are kept by default)",
    )
    parser.add_argument("--retries", type=int, default=1, help="Retries for subprocess transient failures")
    return parser


def slugify(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "-", text.strip())
    clean = re.sub(r"-+", "-", clean).strip("-")
    return clean or "episode"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[podcast-workflow] {msg}", flush=True)


def fmt_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def resolve_executable(name_or_path: str) -> str | None:
    candidate = Path(name_or_path)
    if candidate.exists() and candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(name_or_path)


def require_file(path_str: str, desc: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise WorkflowError(f"missing {desc}: {path}")
    return path


def preflight(args: argparse.Namespace) -> tuple[str, str, str]:
    missing: list[str] = []

    yt_dlp_bin = resolve_executable("yt-dlp")
    ffmpeg_bin = resolve_executable("ffmpeg")
    whisper_bin = resolve_executable(args.whisper_bin)

    if yt_dlp_bin is None:
        missing.append("yt-dlp")
    if ffmpeg_bin is None:
        missing.append("ffmpeg")
    if whisper_bin is None:
        missing.append(f"whisper-cli ({args.whisper_bin})")

    if missing:
        lines = ["dependency check failed:"]
        for dep in missing:
            lines.append(f"- missing: {dep}")
            plain = dep.split(" ")[0]
            if plain in INSTALL_HINTS:
                lines.append("  install:")
                for hint in INSTALL_HINTS[plain]:
                    lines.append(f"    {hint}")
        raise WorkflowError("\n".join(lines))

    require_file(args.asr_model, "ASR model")
    if args.diarization:
        require_file(args.tdrz_model, "tinydiarize model")

    if args.threads <= 0:
        raise WorkflowError("--threads must be > 0")
    if args.progress_interval <= 0:
        raise WorkflowError("--progress-interval must be > 0")
    return yt_dlp_bin or "yt-dlp", ffmpeg_bin or "ffmpeg", whisper_bin or args.whisper_bin


def run_cmd(
    cmd: list[str],
    *,
    retries: int = 0,
    retry_wait_s: float = 1.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    last_err: WorkflowError | None = None
    for attempt in range(retries + 1):
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
        if proc.returncode == 0:
            return proc

        err_msg = (
            f"command failed (attempt {attempt + 1}/{retries + 1}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
        last_err = WorkflowError(err_msg)

        if attempt < retries:
            time.sleep(retry_wait_s * (attempt + 1))

    raise last_err or WorkflowError("unknown command failure")


WHISPER_TS_RE = re.compile(
    r"\[(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?\s*-->\s*(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?\]"
)
WHISPER_PROGRESS_RE = re.compile(r"progress\s*=\s*([0-9]{1,3})%")
WHISPER_TOTAL_SEC_RE = re.compile(r"\((?:\d+)\s+samples,\s*([0-9]+(?:\.[0-9]+)?)\s*sec\)")


def hms_to_ms(hh: str, mm: str, ss: str, msec: str | None) -> int:
    ms = int((msec or "0").ljust(3, "0")[:3])
    return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + ms


def maybe_wrap_with_caffeinate(cmd: list[str], keep_awake: bool) -> tuple[list[str], bool]:
    if not keep_awake or sys.platform != "darwin":
        return cmd, False
    caffeinate_bin = resolve_executable("caffeinate")
    if caffeinate_bin is None:
        log("keep-awake requested but caffeinate is unavailable; continuing without it")
        return cmd, False
    return [caffeinate_bin, "-dimsu", *cmd], True


def run_cmd_live(
    cmd: list[str],
    *,
    stage_name: str,
    retries: int = 0,
    retry_wait_s: float = 1.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    heartbeat_s: int = DEFAULT_PROGRESS_INTERVAL_S,
    expected_audio_ms: int | None = None,
    keep_awake: bool = False,
) -> subprocess.CompletedProcess[str]:
    wrapped_cmd, caffeinated = maybe_wrap_with_caffeinate(cmd, keep_awake)
    if caffeinated:
        log(f"{stage_name}: keep-awake enabled (caffeinate)")

    last_err: WorkflowError | None = None
    for attempt in range(retries + 1):
        proc = subprocess.Popen(
            wrapped_cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
            env=env,
            bufsize=1,
        )
        assert proc.stdout is not None

        output_lines: list[str] = []
        stage_start = time.perf_counter()
        last_report = stage_start
        seen_total_ms = expected_audio_ms
        seen_end_ms = 0
        seen_pct: float | None = None

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)

        while True:
            events = sel.select(timeout=0.5)
            if events:
                line = proc.stdout.readline()
                if line:
                    output_lines.append(line)

                    m_tot = WHISPER_TOTAL_SEC_RE.search(line)
                    if m_tot:
                        try:
                            seen_total_ms = int(float(m_tot.group(1)) * 1000)
                        except ValueError:
                            pass

                    m_ts = WHISPER_TS_RE.search(line)
                    if m_ts:
                        end_ms = hms_to_ms(m_ts.group(5), m_ts.group(6), m_ts.group(7), m_ts.group(8))
                        if end_ms > seen_end_ms:
                            seen_end_ms = end_ms

                    m_pct = WHISPER_PROGRESS_RE.search(line)
                    if m_pct:
                        try:
                            seen_pct = float(m_pct.group(1))
                        except ValueError:
                            pass

            now = time.perf_counter()
            if now - last_report >= heartbeat_s:
                elapsed = now - stage_start
                if seen_total_ms and seen_total_ms > 0 and seen_end_ms > 0:
                    pct = min(100.0, max(0.0, seen_end_ms * 100.0 / seen_total_ms))
                    log(
                        f"{stage_name} progress: ~{pct:.1f}% "
                        f"(audio {fmt_elapsed(seen_end_ms / 1000)}/{fmt_elapsed(seen_total_ms / 1000)}, "
                        f"elapsed {fmt_elapsed(elapsed)})"
                    )
                elif seen_pct is not None:
                    log(f"{stage_name} progress: ~{seen_pct:.1f}% (elapsed {fmt_elapsed(elapsed)})")
                else:
                    log(f"{stage_name} progress: running (elapsed {fmt_elapsed(elapsed)})")
                last_report = now

            if proc.poll() is not None:
                while True:
                    tail = proc.stdout.readline()
                    if not tail:
                        break
                    output_lines.append(tail)
                break

        sel.unregister(proc.stdout)
        proc.stdout.close()
        rc = proc.returncode or 0
        stdout = "".join(output_lines)

        if rc == 0:
            elapsed = time.perf_counter() - stage_start
            log(f"{stage_name} completed (elapsed {fmt_elapsed(elapsed)})")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        err_msg = (
            f"command failed (attempt {attempt + 1}/{retries + 1}): {' '.join(cmd)}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n"
        )
        last_err = WorkflowError(err_msg)
        if attempt < retries:
            time.sleep(retry_wait_s * (attempt + 1))

    raise last_err or WorkflowError("unknown command failure")


def probe_audio_duration_ms(audio_path: Path) -> int | None:
    ffprobe_bin = resolve_executable("ffprobe")
    if ffprobe_bin is None:
        return None
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(audio_path),
    ]
    try:
        proc = run_cmd(cmd, retries=0)
    except WorkflowError:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return int(float(raw) * 1000)
    except ValueError:
        return None


def parse_json_from_maybe_noisy_stdout(raw: str) -> dict[str, Any]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise WorkflowError("failed to parse JSON from yt-dlp output")


def parse_release_ts(entry: dict[str, Any]) -> int:
    for key in ("release_timestamp", "timestamp"):
        val = entry.get(key)
        if isinstance(val, int):
            return val

    upload_date = entry.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit():
        try:
            dt = datetime.strptime(upload_date, "%Y%m%d")
            return int(dt.timestamp())
        except ValueError:
            return 0
    return 0


def inspect_source(yt_dlp_bin: str, url: str, retries: int) -> dict[str, Any]:
    cmd = [yt_dlp_bin, "--dump-single-json", "--skip-download", "--no-warnings", url]
    proc = run_cmd(cmd, retries=retries)
    return parse_json_from_maybe_noisy_stdout(proc.stdout)


def classify_source(info: dict[str, Any]) -> str:
    entries = info.get("entries")
    if isinstance(entries, list) and entries:
        return "show"
    return "episode"


def extract_candidates_from_show(info: dict[str, Any], source_url: str) -> list[EpisodeCandidate]:
    entries = info.get("entries")
    if not isinstance(entries, list) or not entries:
        raise WorkflowError("show page does not contain playable entries")

    candidates: list[EpisodeCandidate] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue

        title = str(entry.get("title") or f"Episode {idx + 1}")
        release_ts = parse_release_ts(entry)
        duration = entry.get("duration")
        duration_s = int(duration) if isinstance(duration, (int, float)) else None
        uploader = entry.get("uploader") if isinstance(entry.get("uploader"), str) else None

        ep_url = entry.get("webpage_url") or entry.get("original_url")
        if not isinstance(ep_url, str) or not ep_url.startswith("http"):
            ep_url = source_url

        playlist_index = entry.get("playlist_index")
        if not isinstance(playlist_index, int) or playlist_index <= 0:
            playlist_index = idx + 1

        candidates.append(
            EpisodeCandidate(
                title=title,
                source_url=ep_url,
                playlist_index=playlist_index,
                release_ts=release_ts,
                duration_s=duration_s,
                uploader=uploader,
            )
        )

    candidates.sort(key=lambda c: (c.release_ts, c.playlist_index or 0), reverse=True)
    return candidates


def choose_episode(
    candidates: list[EpisodeCandidate],
    episode_index: int | None,
    *,
    input_fn: Callable[[str], str] = input,
) -> EpisodeCandidate:
    display = candidates[:10]
    if not display:
        raise WorkflowError("no episode candidate found")

    if episode_index is not None:
        if episode_index < 1 or episode_index > len(display):
            raise WorkflowError(f"--episode-index must be in [1, {len(display)}]")
        return display[episode_index - 1]

    if not sys.stdin.isatty():
        raise WorkflowError("show URL requires --episode-index in non-interactive environment")

    log("detected show page; choose one episode from recent list:")
    for i, ep in enumerate(display, start=1):
        ts = datetime.fromtimestamp(ep.release_ts).strftime("%Y-%m-%d") if ep.release_ts > 0 else "unknown-date"
        dur = f"{ep.duration_s // 60}m" if ep.duration_s else "unknown-duration"
        log(f"{i}. {ep.title} ({ts}, {dur})")

    while True:
        raw = input_fn("请输入要处理的集数序号 [1-10]: ").strip()
        if not raw.isdigit():
            print("请输入数字。")
            continue
        picked = int(raw)
        if 1 <= picked <= len(display):
            return display[picked - 1]
        print(f"请输入 1 到 {len(display)} 之间的数字。")


def discover_profile_files(profile_dir: Path) -> list[Path]:
    if not profile_dir.exists() or not profile_dir.is_dir():
        return []
    return sorted(
        p for p in profile_dir.glob("*.profile.json") if p.is_file() and not p.name.startswith("_")
    )


def parse_profile_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item)
    return result


def parse_profile_replacements(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, repl in value.items():
        if isinstance(key, str) and key and isinstance(repl, str):
            result[key] = repl
    return result


def load_profile_from_file(path: Path) -> PodcastProfile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"failed to load profile {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise WorkflowError(f"profile must be a JSON object: {path}")

    match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name") else path.stem

    return PodcastProfile(
        name=name,
        path=path,
        source_url_regex=match.get("source_url_regex") if isinstance(match.get("source_url_regex"), str) else None,
        title_regex=match.get("title_regex") if isinstance(match.get("title_regex"), str) else None,
        input_url_regex=match.get("input_url_regex") if isinstance(match.get("input_url_regex"), str) else None,
        speaker_a_name=raw.get("speaker_a_name") if isinstance(raw.get("speaker_a_name"), str) else None,
        speaker_b_name=raw.get("speaker_b_name") if isinstance(raw.get("speaker_b_name"), str) else None,
        noise_phrases=parse_profile_text_list(raw.get("noise_phrases")),
        replacements=parse_profile_replacements(raw.get("replacements")),
    )


def regex_matches(pattern: str | None, text: str) -> bool:
    if not pattern:
        return True
    return re.search(pattern, text) is not None


def profile_matches(profile: PodcastProfile, *, input_url: str, source_url: str, title: str) -> bool:
    has_matcher = any([profile.input_url_regex, profile.source_url_regex, profile.title_regex])
    if not has_matcher:
        return False
    return (
        regex_matches(profile.input_url_regex, input_url)
        and regex_matches(profile.source_url_regex, source_url)
        and regex_matches(profile.title_regex, title)
    )


def resolve_profile(
    *,
    explicit_profile: str | None,
    profile_dir: str,
    input_url: str,
    selected_episode: EpisodeCandidate,
) -> PodcastProfile | None:
    profile_root = Path(profile_dir)

    if explicit_profile:
        candidate = Path(explicit_profile)
        if not candidate.exists():
            if explicit_profile.endswith(".json"):
                candidate = profile_root / explicit_profile
            else:
                candidate = profile_root / f"{explicit_profile}.profile.json"
        if not candidate.exists():
            raise WorkflowError(f"profile not found: {explicit_profile}")
        profile = load_profile_from_file(candidate)
        log(f"using explicit profile: {profile.name} ({candidate})")
        return profile

    for path in discover_profile_files(profile_root):
        profile = load_profile_from_file(path)
        if profile_matches(
            profile,
            input_url=input_url,
            source_url=selected_episode.source_url,
            title=selected_episode.title,
        ):
            log(f"matched profile: {profile.name}")
            return profile
    return None


def clone_segments(segments: list[Segment]) -> list[Segment]:
    return [
        Segment(
            t0_ms=seg.t0_ms,
            t1_ms=seg.t1_ms,
            text=seg.text,
            speaker=seg.speaker,
            speaker_turn_next=seg.speaker_turn_next,
        )
        for seg in segments
    ]


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    rendered = text
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)
    return rendered


def apply_profile_to_segments(segments: list[Segment], profile: PodcastProfile | None) -> list[Segment]:
    if not profile:
        return segments

    result: list[Segment] = []
    for seg in clone_segments(segments):
        if profile.noise_phrases and any(phrase in seg.text for phrase in profile.noise_phrases):
            continue
        seg.text = apply_replacements(seg.text, profile.replacements)
        result.append(seg)
    return result


def speaker_name_map(args: argparse.Namespace, profile: PodcastProfile | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if profile:
        if profile.speaker_a_name:
            mapping["Speaker A"] = profile.speaker_a_name
        if profile.speaker_b_name:
            mapping["Speaker B"] = profile.speaker_b_name
    if args.speaker_a_name:
        mapping["Speaker A"] = args.speaker_a_name
    if args.speaker_b_name:
        mapping["Speaker B"] = args.speaker_b_name
    return mapping


def find_downloaded_file(stdout: str, temp_dir: Path) -> Path:
    for line in reversed([line.strip() for line in stdout.splitlines()]):
        if not line:
            continue
        p = Path(line)
        if p.exists() and p.is_file():
            return p

    files = sorted([p for p in temp_dir.glob("*") if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
    if files:
        return files[0]

    raise WorkflowError("download finished but no media file found")


def download_audio(
    yt_dlp_bin: str,
    *,
    input_url: str,
    source_type: str,
    selected_episode: EpisodeCandidate,
    temp_dir: Path,
    retries: int,
) -> Path:
    output_tpl = temp_dir / "source.%(ext)s"
    cmd = [
        yt_dlp_bin,
        "-f",
        "bestaudio/best",
        "--no-warnings",
        "--restrict-filenames",
        "--print",
        "after_move:filepath",
        "-o",
        str(output_tpl),
    ]

    if source_type == "show" and selected_episode.playlist_index:
        cmd += ["--yes-playlist", "--playlist-items", str(selected_episode.playlist_index), input_url]
    else:
        cmd += ["--no-playlist", selected_episode.source_url]

    proc = run_cmd(cmd, retries=retries)
    return find_downloaded_file(proc.stdout, temp_dir)


def transcode_to_mp3(ffmpeg_bin: str, src: Path, dst_mp3: Path, retries: int) -> None:
    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(dst_mp3),
    ]
    run_cmd(cmd, retries=retries)


def build_whisper_cmd(
    *,
    whisper_bin: str,
    model_path: str,
    audio_path: Path,
    out_prefix: Path,
    language: str,
    threads: int,
    output_txt: bool = False,
    output_srt: bool = False,
    output_json: bool = True,
    tinydiarize: bool = False,
    print_progress: bool = True,
    use_gpu: bool = True,
) -> list[str]:
    # Stable command profile:
    # whisper-cli -m <model> -f <audio.mp3> -l zh -t 8 -mc 0 -oj -of <prefix> -pp
    # add -ng only when GPU is explicitly disabled
    cmd = [
        whisper_bin,
        "-m",
        model_path,
        "-f",
        str(audio_path),
        "-l",
        language,
        "-t",
        str(threads),
        "-mc",
        "0",
        "-of",
        str(out_prefix),
    ]
    if not use_gpu:
        cmd.append("-ng")
    if output_txt:
        cmd.append("-otxt")
    if output_srt:
        cmd.append("-osrt")
    if output_json:
        cmd.append("-oj")
    if tinydiarize:
        cmd.append("-tdrz")
    if print_progress:
        cmd.append("-pp")
    return cmd


def run_whisper_json(
    whisper_bin: str,
    *,
    model_path: str,
    audio_path: Path,
    out_prefix: Path,
    language: str,
    threads: int,
    retries: int,
    tinydiarize: bool = False,
    use_gpu: bool = True,
    progress_interval_s: int = DEFAULT_PROGRESS_INTERVAL_S,
    expected_audio_ms: int | None = None,
    keep_awake: bool = False,
) -> dict[str, Any]:
    cmd = build_whisper_cmd(
        whisper_bin=whisper_bin,
        model_path=model_path,
        audio_path=audio_path,
        out_prefix=out_prefix,
        language=language,
        threads=threads,
        output_txt=False,
        output_srt=False,
        output_json=True,
        tinydiarize=tinydiarize,
        print_progress=True,
        use_gpu=use_gpu,
    )
    run_cmd_live(
        cmd,
        stage_name="tinydiarize",
        retries=retries,
        heartbeat_s=progress_interval_s,
        expected_audio_ms=expected_audio_ms,
        keep_awake=keep_awake,
    )
    json_path = Path(f"{out_prefix}.json")
    if not json_path.exists():
        raise WorkflowError(f"whisper output missing: {json_path}")
    with json_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def run_whisper_transcription(
    whisper_bin: str,
    *,
    model_path: str,
    audio_path: Path,
    out_dir: Path,
    language: str,
    threads: int,
    retries: int,
    use_gpu: bool = True,
    progress_interval_s: int = DEFAULT_PROGRESS_INTERVAL_S,
    expected_audio_ms: int | None = None,
    keep_awake: bool = False,
) -> TranscriptionResult:
    out_prefix = out_dir / "01_transcript"
    cmd = build_whisper_cmd(
        whisper_bin=whisper_bin,
        model_path=model_path,
        audio_path=audio_path,
        out_prefix=out_prefix,
        language=language,
        threads=threads,
        output_txt=False,
        output_srt=False,
        output_json=True,
        use_gpu=use_gpu,
    )
    run_cmd_live(
        cmd,
        stage_name="transcription",
        retries=retries,
        heartbeat_s=progress_interval_s,
        expected_audio_ms=expected_audio_ms,
        keep_awake=keep_awake,
    )

    json_path = Path(f"{out_prefix}.json")

    for p in (json_path,):
        if not p.exists():
            raise WorkflowError(f"whisper output missing: {p}")

    with json_path.open("r", encoding="utf-8") as f:
        transcript_json = json.load(f)

    return TranscriptionResult(
        command=cmd,
        transcript_json=transcript_json,
    )


def language_from_whisper_json(transcript_json: dict[str, Any], fallback: str) -> str:
    result = transcript_json.get("result")
    if isinstance(result, dict):
        lang = result.get("language")
        if isinstance(lang, str) and lang:
            return lang.lower()
    return fallback.lower()


def parse_segments(transcript_json: dict[str, Any]) -> list[Segment]:
    result: list[Segment] = []
    for item in transcript_json.get("transcription", []):
        if not isinstance(item, dict):
            continue
        offsets = item.get("offsets") if isinstance(item.get("offsets"), dict) else {}
        t0 = int(offsets.get("from", 0))
        t1 = int(offsets.get("to", t0))
        text = str(item.get("text") or "").strip()
        speaker = item.get("speaker") if isinstance(item.get("speaker"), str) else None
        speaker_turn_next = bool(item.get("speaker_turn_next", False))
        if not text and not speaker_turn_next:
            continue
        result.append(
            Segment(
                t0_ms=t0,
                t1_ms=t1,
                text=text,
                speaker=speaker,
                speaker_turn_next=speaker_turn_next,
            )
        )
    return result


def assign_turn_speakers(segments: list[Segment]) -> None:
    if not segments:
        return
    speaker = "Speaker A"
    for seg in segments:
        if not seg.speaker:
            seg.speaker = speaker
        if seg.speaker_turn_next:
            speaker = "Speaker B" if speaker == "Speaker A" else "Speaker A"


def overlap_ms(a0: int, a1: int, b0: int, b1: int) -> int:
    lo = max(a0, b0)
    hi = min(a1, b1)
    return max(0, hi - lo)


def merge_speaker_labels(main_segments: list[Segment], tdrz_segments: list[Segment]) -> None:
    if not main_segments or not tdrz_segments:
        return

    for main in main_segments:
        best: Segment | None = None
        best_ov = -1
        for dia in tdrz_segments:
            ov = overlap_ms(main.t0_ms, main.t1_ms, dia.t0_ms, dia.t1_ms)
            if ov > best_ov:
                best_ov = ov
                best = dia

        if best_ov > 0 and best and best.speaker:
            main.speaker = best.speaker
            continue

        main_center = (main.t0_ms + main.t1_ms) / 2.0
        nearest = min(tdrz_segments, key=lambda s: abs(((s.t0_ms + s.t1_ms) / 2.0) - main_center))
        main.speaker = nearest.speaker


def normalize_text_for_compare(text: str) -> str:
    # Keep CJK / letters / digits and drop spacing noise for rough similarity checks.
    return re.sub(r"\s+", "", text).lower()


def assess_diarization_quality(
    main_segments: list[Segment],
    diar_segments: list[Segment],
    detected_lang: str,
) -> DiarizationAssessment:
    if not main_segments or not diar_segments:
        return DiarizationAssessment(
            text_similarity_ratio=None,
            temporal_coverage_ratio=None,
            speaker_turn_markers=0,
            labeled_segment_ratio=0.0,
            second_full_asr_recommended=False,
            note="missing segments for diarization assessment",
        )

    main_text = normalize_text_for_compare("".join(s.text for s in main_segments if s.text))
    diar_text = normalize_text_for_compare("".join(s.text for s in diar_segments if s.text))

    ratio: float | None = None
    max_len = 120_000
    if main_text and diar_text:
        ratio = round(difflib.SequenceMatcher(None, main_text[:max_len], diar_text[:max_len]).ratio(), 4)

    main_start = min(s.t0_ms for s in main_segments)
    main_end = max(s.t1_ms for s in main_segments)
    diar_start = min(s.t0_ms for s in diar_segments)
    diar_end = max(s.t1_ms for s in diar_segments)
    temporal_cov = None
    if main_end > main_start:
        temporal_cov = round(overlap_ms(main_start, main_end, diar_start, diar_end) / (main_end - main_start), 4)

    labeled_count = sum(1 for s in main_segments if s.speaker)
    labeled_ratio = round(labeled_count / len(main_segments), 4)
    turn_markers = sum(1 for s in diar_segments if s.speaker_turn_next)

    if detected_lang.startswith("en"):
        recommendation = labeled_ratio < 0.9 or (temporal_cov is not None and temporal_cov < 0.9)
        note = "english audio: diarization text is relatively comparable"
    else:
        recommendation = labeled_ratio < 0.9 or (temporal_cov is not None and temporal_cov < 0.9)
        note = "non-english audio: tdrz text is for speaker turns only; main ASR text remains authoritative"

    return DiarizationAssessment(
        text_similarity_ratio=ratio,
        temporal_coverage_ratio=temporal_cov,
        speaker_turn_markers=turn_markers,
        labeled_segment_ratio=labeled_ratio,
        second_full_asr_recommended=recommendation,
        note=note,
    )


def fmt_ms(ms: int) -> str:
    seconds = max(0, ms // 1000)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


TURN_END_PUNCT = "。！？!?…"
TURN_INLINE_PUNCT = "，；：、,;:"
TURN_LEADING_PUNCT = "，。！？；：、,.!?;:)]）】〉》」』”’"
TURN_WRAP_CHARS = 100


def clean_turn_fragment(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def render_turn_text(parts: list[str]) -> str:
    rendered = ""
    for raw in parts:
        part = clean_turn_fragment(raw).strip(" ，。；;")
        if not part:
            continue
        if not rendered:
            rendered = part
            continue
        if rendered[-1] in TURN_END_PUNCT + TURN_INLINE_PUNCT or part[0] in TURN_LEADING_PUNCT:
            rendered += part
        else:
            rendered += "，" + part

    rendered = re.sub(r"，{2,}", "，", rendered)
    rendered = re.sub(r"([。！？!?…])，", r"\1", rendered)
    if rendered and rendered[-1] not in TURN_END_PUNCT:
        rendered += "。"
    return rendered


def split_keep_punct(text: str, punctuation: str) -> list[str]:
    pieces: list[str] = []
    current = ""
    for ch in text:
        current += ch
        if ch in punctuation:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces


def hard_wrap_text(text: str, max_chars: int) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    return [clean[i : i + max_chars] for i in range(0, len(clean), max_chars)]


def pack_chunks(chunks: list[str], max_chars: int) -> list[str]:
    lines: list[str] = []
    current = ""

    for raw in chunks:
        chunk = raw.strip()
        if not chunk:
            continue

        if len(chunk) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.extend(hard_wrap_text(chunk, max_chars))
            continue

        if not current:
            current = chunk
            continue

        if len(current) + len(chunk) <= max_chars:
            current += chunk
        else:
            lines.append(current)
            current = chunk

    if current:
        lines.append(current)

    return lines


def wrap_turn_text(text: str, max_chars: int = TURN_WRAP_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    sentence_chunks = split_keep_punct(text, TURN_END_PUNCT)
    if len(sentence_chunks) == 1:
        clause_chunks = split_keep_punct(text, TURN_INLINE_PUNCT)
        return pack_chunks(clause_chunks, max_chars)

    lines: list[str] = []
    current = ""

    for sentence in sentence_chunks:
        clause_lines = pack_chunks(split_keep_punct(sentence, TURN_INLINE_PUNCT), max_chars)
        for clause_line in clause_lines:
            if not current:
                current = clause_line
                continue

            if len(current) + len(clause_line) <= max_chars:
                current += clause_line
            else:
                lines.append(current)
                current = clause_line

    if current:
        lines.append(current)

    return lines


def merge_segments_into_turns(segments: list[Segment]) -> list[SpeakerTurn]:
    turns: list[SpeakerTurn] = []
    current: SpeakerTurn | None = None

    for seg in segments:
        text = clean_turn_fragment(seg.text)
        if not text and current is None:
            continue

        if current and current.speaker == seg.speaker:
            current.t1_ms = seg.t1_ms
            if text:
                current.parts.append(text)
            continue

        if current:
            turns.append(current)
        current = SpeakerTurn(
            t0_ms=seg.t0_ms,
            t1_ms=seg.t1_ms,
            speaker=seg.speaker,
            parts=[text] if text else [],
        )

    if current:
        turns.append(current)

    return [turn for turn in turns if turn.parts]


def transcript_markdown(
    segments: list[Segment],
    *,
    source_url: str,
    episode_title: str,
    language: str,
    speaker_names: dict[str, str] | None = None,
) -> str:
    lines = [
        "# 转写稿",
        "",
        f"- 生成时间: {now_iso()}",
        f"- 来源链接: {source_url}",
        f"- 集标题: {episode_title}",
        f"- 识别语言: {language}",
        "",
        "## 正文",
        "",
    ]

    if any(seg.speaker for seg in segments):
        for turn in merge_segments_into_turns(segments):
            speaker = speaker_names.get(turn.speaker, turn.speaker) if speaker_names and turn.speaker else turn.speaker
            ts = f"{fmt_ms(turn.t0_ms)} - {fmt_ms(turn.t1_ms)}"
            text = render_turn_text(turn.parts)
            if speaker:
                lines.append(f"{speaker}（{ts}）：")
            else:
                lines.append(f"（{ts}）")
            lines.extend(wrap_turn_text(text))
            lines.append("")
    else:
        for seg in segments:
            ts = f"[{fmt_ms(seg.t0_ms)} - {fmt_ms(seg.t1_ms)}]"
            lines.append(f"- {ts} {seg.text}")

    lines.append("")
    return "\n".join(lines)


def execute_workflow(args: argparse.Namespace, *, input_fn: Callable[[str], str] = input) -> Path:
    t_start = time.perf_counter()
    t_mark = t_start

    yt_dlp_bin, ffmpeg_bin, whisper_bin = preflight(args)

    info = inspect_source(yt_dlp_bin, args.url, args.retries)
    source_type = classify_source(info)

    if source_type == "show":
        candidates = extract_candidates_from_show(info, args.url)
        selected = choose_episode(candidates, args.episode_index, input_fn=input_fn)
    else:
        selected = EpisodeCandidate(
            title=str(info.get("title") or "Episode"),
            source_url=str(info.get("webpage_url") or info.get("original_url") or args.url),
            playlist_index=None,
            release_ts=parse_release_ts(info),
            duration_s=int(info["duration"]) if isinstance(info.get("duration"), (int, float)) else None,
            uploader=info.get("uploader") if isinstance(info.get("uploader"), str) else None,
        )
    profile = resolve_profile(
        explicit_profile=args.profile,
        profile_dir=args.profile_dir,
        input_url=args.url,
        selected_episode=selected,
    )
    speaker_names = speaker_name_map(args, profile)

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(selected.title)[:48]}"
    out_dir = Path(args.out_root).resolve() / run_id
    out_dir.mkdir(parents=True, exist_ok=False)

    stage_times: dict[str, float] = {}

    with tempfile.TemporaryDirectory(prefix="podcast-workflow-") as tmp:
        tmp_dir = Path(tmp)

        log("downloading audio source")
        source_audio = download_audio(
            yt_dlp_bin,
            input_url=args.url,
            source_type=source_type,
            selected_episode=selected,
            temp_dir=tmp_dir,
            retries=args.retries,
        )
        stage_times["download_s"] = round(time.perf_counter() - t_mark, 3)
        t_mark = time.perf_counter()

        log("transcoding to MP3")
        audio_mp3 = out_dir / "audio.mp3"
        transcode_to_mp3(ffmpeg_bin, source_audio, audio_mp3, retries=args.retries)
        stage_times["transcode_s"] = round(time.perf_counter() - t_mark, 3)
        t_mark = time.perf_counter()
        expected_audio_ms = probe_audio_duration_ms(audio_mp3)
        if expected_audio_ms is None and selected.duration_s:
            expected_audio_ms = int(selected.duration_s * 1000)
        if expected_audio_ms:
            log(f"detected audio duration: {fmt_elapsed(expected_audio_ms / 1000)}")

        log("running whisper transcription")
        asr = run_whisper_transcription(
            whisper_bin,
            model_path=args.asr_model,
            audio_path=audio_mp3,
            out_dir=out_dir,
            language=args.language,
            threads=args.threads,
            retries=args.retries,
            use_gpu=bool(args.gpu),
            progress_interval_s=args.progress_interval,
            expected_audio_ms=expected_audio_ms,
            keep_awake=bool(args.keep_awake),
        )
        stage_times["asr_s"] = round(time.perf_counter() - t_mark, 3)
        t_mark_diar = time.perf_counter()
        if args.diarization:
            log("running tinydiarize diarization pass")
            diar_json = run_whisper_json(
                whisper_bin,
                model_path=args.tdrz_model,
                audio_path=audio_mp3,
                out_prefix=out_dir / "01_diarization_tdrz",
                language="en",
                threads=args.threads,
                retries=args.retries,
                tinydiarize=True,
                use_gpu=bool(args.gpu),
                progress_interval_s=args.progress_interval,
                expected_audio_ms=expected_audio_ms,
                keep_awake=bool(args.keep_awake),
            )
            diar_segments = parse_segments(diar_json)
            assign_turn_speakers(diar_segments)
        else:
            diar_segments = []
        stage_times["diarization_s"] = round(time.perf_counter() - t_mark_diar, 3)

    segments = parse_segments(asr.transcript_json)
    if args.diarization and diar_segments:
        merge_speaker_labels(segments, diar_segments)
    segments = apply_profile_to_segments(segments, profile)
    detected_lang = language_from_whisper_json(asr.transcript_json, fallback=args.language)
    if args.diarization and diar_segments:
        diar_assessment = assess_diarization_quality(segments, diar_segments, detected_lang)
    else:
        diar_assessment = DiarizationAssessment(
            text_similarity_ratio=None,
            temporal_coverage_ratio=None,
            speaker_turn_markers=0,
            labeled_segment_ratio=0.0,
            second_full_asr_recommended=False,
            note="diarization disabled or unavailable",
        )
    log(
        "diarization assessment: "
        f"labeled={diar_assessment.labeled_segment_ratio:.2%}, "
        f"coverage={diar_assessment.temporal_coverage_ratio}, "
        f"text_similarity={diar_assessment.text_similarity_ratio}, "
        f"second_full_asr_recommended={diar_assessment.second_full_asr_recommended}"
    )

    transcript_md = transcript_markdown(
        segments,
        source_url=args.url,
        episode_title=selected.title,
        language=detected_lang,
        speaker_names=speaker_names,
    )
    transcript_path = out_dir / "01_transcript.md"
    transcript_path.write_text(transcript_md, encoding="utf-8")

    elapsed = round(time.perf_counter() - t_start, 3)
    if args.keep_json_artifacts:
        manifest = {
            "run_id": run_id,
            "created_at": now_iso(),
            "input_url": args.url,
            "source_type": source_type,
            "selected_episode": {
                "title": selected.title,
                "source_url": selected.source_url,
                "playlist_index": selected.playlist_index,
                "release_ts": selected.release_ts,
                "duration_s": selected.duration_s,
                "uploader": selected.uploader,
            },
            "profile": {
                "name": profile.name,
                "path": str(profile.path),
            }
            if profile
            else None,
            "models": {
                "asr_model": str(Path(args.asr_model)),
                "whisper_bin": whisper_bin,
                "gpu_enabled": bool(args.gpu),
                "keep_awake": bool(args.keep_awake),
            },
            "asr": {
                "language": detected_lang,
                "segment_count": len(segments),
                "threads": args.threads,
                "command": asr.command,
                "diarization_requested": bool(args.diarization),
                "diarization_applied": bool(args.diarization and diar_segments),
                "diarization_model": args.tdrz_model if args.diarization else None,
                "progress_interval_s": int(args.progress_interval),
                "diarization_assessment": {
                    "text_similarity_ratio": diar_assessment.text_similarity_ratio,
                    "temporal_coverage_ratio": diar_assessment.temporal_coverage_ratio,
                    "speaker_turn_markers": diar_assessment.speaker_turn_markers,
                    "labeled_segment_ratio": diar_assessment.labeled_segment_ratio,
                    "second_full_asr_recommended": diar_assessment.second_full_asr_recommended,
                    "note": diar_assessment.note,
                },
            },
            "timing_seconds": stage_times,
            "elapsed_seconds": elapsed,
            "artifacts": {
                "audio_mp3": "audio.mp3",
                "transcript_md": "01_transcript.md",
                "transcript_json": "01_transcript.json",
                "diarization_json": "01_diarization_tdrz.json" if args.diarization and diar_segments else None,
            },
        }
        manifest_path = out_dir / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        for extra in [
            out_dir / "run_manifest.json",
        ]:
            try:
                if extra.exists():
                    extra.unlink()
            except OSError:
                pass

    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        out_dir = execute_workflow(args)
    except WorkflowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130

    log(f"workflow completed successfully: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
