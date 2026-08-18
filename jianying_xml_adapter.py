from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pyJianYingDraft import DraftCryptoConfig, JianyingDraftCryptoCodec

from bridge_safety import BRIDGE_VERSION, PROVENANCE_FILENAME, sampled_media_fingerprint


US_PER_SECOND = 1_000_000


class GateFailure(RuntimeError):
    pass


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def frame_to_us(frame: int, fps: int) -> int:
    if frame < 0:
        raise ValueError("frame must be non-negative")
    if fps <= 0:
        raise ValueError("fps must be positive")
    return (frame * US_PER_SECOND + fps // 2) // fps


def us_to_frame(value_us: int, fps: int) -> int:
    if value_us < 0:
        raise ValueError("microseconds must be non-negative")
    if fps <= 0:
        raise ValueError("fps must be positive")
    return (value_us * fps + US_PER_SECOND // 2) // US_PER_SECOND


def checked_us_to_frame(value_us: int, fps: int, label: str, tolerance_us: int = 1000) -> int:
    frame = us_to_frame(value_us, fps)
    canonical = frame_to_us(frame, fps)
    if abs(value_us - canonical) > tolerance_us:
        raise GateFailure(
            f"{label}={value_us}us is off the {fps}fps frame grid by more than {tolerance_us}us"
        )
    return frame


def _trange(start_frame: int, end_frame: int, fps: int) -> Dict[str, int]:
    start_us = frame_to_us(start_frame, fps)
    end_us = frame_to_us(end_frame, fps)
    result = {"duration": end_us - start_us}
    if start_us:
        result["start"] = start_us
    return result


def _norm_jy_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _write_json_atomic(path: Path, payload: Dict[str, Any], *, compact: bool = False) -> None:
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _write_bytes_atomic(path, text.encode("utf-8"))


def _read_json(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON top level is not an object: {path}")
    return data


def _text(node: Optional[ET.Element], name: str) -> Optional[str]:
    if node is None:
        return None
    child = node.find(name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _required_int(node: ET.Element, name: str, label: str) -> int:
    value = _text(node, name)
    if value is None:
        raise GateFailure(f"{label} missing <{name}>")
    try:
        return int(value)
    except ValueError as exc:
        raise GateFailure(f"{label} has non-integer <{name}>={value!r}") from exc


def decode_pathurl(pathurl: str) -> Path:
    parsed = urllib.parse.urlsplit(pathurl)
    if parsed.scheme.lower() != "file":
        raise GateFailure(f"unsupported media URL scheme: {pathurl}")
    decoded = urllib.parse.unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:/", decoded):
        decoded = decoded[1:]
    if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
        decoded = f"//{parsed.netloc}{decoded}"
    return Path(decoded.replace("/", os.sep))


@dataclass(frozen=True)
class Clip:
    index: int
    clip_id: str
    file_id: str
    target_start: int
    target_end: int
    source_in: int
    source_out: int

    @property
    def duration(self) -> int:
        return self.target_end - self.target_start


@dataclass(frozen=True)
class SrtCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass
class TimelineIR:
    xml_path: str
    sequence_name: str
    fps: int
    ntsc: bool
    duration_frames: int
    width: int
    height: int
    media_path: str
    xml_media_duration_frames: int
    clips: List[Clip]
    audio_clips: List[Clip]
    identity_effect_counts: Dict[str, int]
    source_gap_frames: List[int]

    @property
    def duration_us(self) -> int:
        return frame_to_us(self.duration_frames, self.fps)

    def serializable(self) -> Dict[str, Any]:
        return {
            "xml_path": self.xml_path,
            "sequence_name": self.sequence_name,
            "fps": self.fps,
            "ntsc": self.ntsc,
            "duration_frames": self.duration_frames,
            "duration_us": self.duration_us,
            "width": self.width,
            "height": self.height,
            "media_path": self.media_path,
            "xml_media_duration_frames": self.xml_media_duration_frames,
            "identity_effect_counts": self.identity_effect_counts,
            "source_gap_frames": self.source_gap_frames,
            "clips": [asdict(clip) for clip in self.clips],
            "audio_clips": [asdict(clip) for clip in self.audio_clips],
        }


def _number(value: Optional[str]) -> float:
    if value is None:
        raise ValueError("missing numeric value")
    return float(value)


def _effect_is_identity(effect: ET.Element) -> bool:
    effect_id = _text(effect, "effectid") or ""
    expected_scalars: Dict[str, Dict[str, float]] = {
        "basic": {"scale": 100.0, "rotation": 0.0},
        "crop": {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0},
        "opacity": {"opacity": 100.0},
        "audiolevels": {"level": 1.0},
        "audiopan": {"pan": 0.0},
    }
    if effect_id not in expected_scalars:
        return False

    params: Dict[str, ET.Element] = {}
    for parameter in effect.findall("parameter"):
        parameter_id = _text(parameter, "parameterid")
        if parameter_id:
            params[parameter_id] = parameter

    try:
        for parameter_id, expected in expected_scalars[effect_id].items():
            value_node = params[parameter_id].find("value")
            actual = _number(value_node.text.strip() if value_node is not None and value_node.text else None)
            if abs(actual - expected) > 1e-9:
                return False
        if effect_id == "basic":
            for parameter_id in ("center", "centerOffset"):
                value = params[parameter_id].find("value")
                if value is None:
                    return False
                if abs(_number(_text(value, "horiz"))) > 1e-9:
                    return False
                if abs(_number(_text(value, "vert"))) > 1e-9:
                    return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _collect_files(root: ET.Element) -> Dict[str, ET.Element]:
    definitions: Dict[str, ET.Element] = {}
    for file_node in root.findall(".//file"):
        file_id = file_node.attrib.get("id", "").strip()
        if not file_id:
            continue
        if file_node.find("pathurl") is not None or file_node.find("duration") is not None:
            definitions[file_id] = file_node
    return definitions


def _parse_clips(nodes: Iterable[ET.Element], label: str) -> List[Clip]:
    clips: List[Clip] = []
    for index, node in enumerate(nodes):
        enabled = (_text(node, "enabled") or "TRUE").upper()
        if enabled == "FALSE":
            raise GateFailure(f"disabled {label} clip is not supported: index={index}")
        file_node = node.find("file")
        if file_node is None or not file_node.attrib.get("id"):
            raise GateFailure(f"{label} clip has no file id: index={index}")
        clips.append(
            Clip(
                index=index,
                clip_id=node.attrib.get("id", f"{label}-{index}"),
                file_id=file_node.attrib["id"],
                target_start=_required_int(node, "start", f"{label}[{index}]"),
                target_end=_required_int(node, "end", f"{label}[{index}]"),
                source_in=_required_int(node, "in", f"{label}[{index}]"),
                source_out=_required_int(node, "out", f"{label}[{index}]"),
            )
        )
    return clips


def parse_fcp7_xml(xml_path: Path) -> TimelineIR:
    root = ET.parse(xml_path).getroot()
    sequence = root.find(".//sequence")
    if sequence is None:
        raise GateFailure("FCP7 XML has no sequence")

    rate_node = sequence.find("rate")
    if rate_node is None:
        raise GateFailure("sequence is missing <rate>")
    fps = _required_int(rate_node, "timebase", "sequence rate")
    ntsc = ((_text(rate_node, "ntsc") or "FALSE").upper() == "TRUE")
    if ntsc:
        raise GateFailure("NTSC fractional frame rates are not supported by this first test")
    duration_frames = _required_int(sequence, "duration", "sequence")

    width_node = sequence.find("./media/video/format/samplecharacteristics/width")
    height_node = sequence.find("./media/video/format/samplecharacteristics/height")
    width = int(width_node.text) if width_node is not None and width_node.text else 0
    height = int(height_node.text) if height_node is not None and height_node.text else 0

    video_tracks = [track for track in sequence.findall("./media/video/track") if track.findall("clipitem")]
    audio_tracks = [track for track in sequence.findall("./media/audio/track") if track.findall("clipitem")]
    if len(video_tracks) != 1 or len(audio_tracks) != 1:
        raise GateFailure(
            f"expected exactly one populated video and audio track, got video={len(video_tracks)} audio={len(audio_tracks)}"
        )

    clips = _parse_clips(video_tracks[0].findall("clipitem"), "video")
    audio_clips = _parse_clips(audio_tracks[0].findall("clipitem"), "audio")
    if not clips:
        raise GateFailure("timeline contains no video clips")
    if len(clips) != len(audio_clips):
        raise GateFailure(f"video/audio clip count differs: {len(clips)} vs {len(audio_clips)}")

    files = _collect_files(root)
    file_ids = {clip.file_id for clip in clips + audio_clips}
    if len(file_ids) != 1:
        raise GateFailure(f"first test requires one source media id, got {sorted(file_ids)}")
    file_id = next(iter(file_ids))
    if file_id not in files:
        raise GateFailure(f"file id has no complete definition: {file_id}")
    file_node = files[file_id]
    pathurl = _text(file_node, "pathurl")
    if not pathurl:
        raise GateFailure(f"file definition has no pathurl: {file_id}")
    media_path = decode_pathurl(pathurl)
    xml_media_duration_frames = _required_int(file_node, "duration", f"file {file_id}")

    effect_counts: Dict[str, int] = {}
    non_identity_effects: List[str] = []
    for effect in sequence.findall(".//filter/effect"):
        effect_id = _text(effect, "effectid") or "<missing>"
        effect_counts[effect_id] = effect_counts.get(effect_id, 0) + 1
        if not _effect_is_identity(effect):
            non_identity_effects.append(effect_id)
    if non_identity_effects:
        raise GateFailure(f"non-identity or unsupported effects present: {sorted(set(non_identity_effects))}")
    if sequence.findall(".//transitionitem"):
        raise GateFailure("transitions are not supported in this test")
    if sequence.findall(".//generatoritem"):
        raise GateFailure("titles/generators are not supported in this test")
    if sequence.findall(".//clipitem/sequence"):
        raise GateFailure("nested sequences are not supported in this test")

    previous_end = 0
    for clip in clips:
        if clip.target_start != previous_end:
            raise GateFailure(
                f"timeline is not continuous at clip {clip.index}: expected {previous_end}, got {clip.target_start}"
            )
        if clip.duration <= 0:
            raise GateFailure(f"non-positive target duration at clip {clip.index}")
        if clip.source_out - clip.source_in != clip.duration:
            raise GateFailure(f"source/target duration mismatch at clip {clip.index}")
        if clip.source_in < 0 or clip.source_out > xml_media_duration_frames:
            raise GateFailure(f"source range out of XML media bounds at clip {clip.index}")
        previous_end = clip.target_end
    if previous_end != duration_frames:
        raise GateFailure(f"last clip ends at {previous_end}, sequence duration is {duration_frames}")

    for video, audio in zip(clips, audio_clips):
        if (
            video.target_start,
            video.target_end,
            video.source_in,
            video.source_out,
            video.file_id,
        ) != (
            audio.target_start,
            audio.target_end,
            audio.source_in,
            audio.source_out,
            audio.file_id,
        ):
            raise GateFailure(f"video/audio pair mismatch at clip {video.index}")

    source_gaps = [clips[i + 1].source_in - clips[i].source_out for i in range(len(clips) - 1)]
    return TimelineIR(
        xml_path=str(xml_path.resolve()),
        sequence_name=_text(sequence, "name") or xml_path.stem,
        fps=fps,
        ntsc=ntsc,
        duration_frames=duration_frames,
        width=width,
        height=height,
        media_path=str(media_path.resolve()),
        xml_media_duration_frames=xml_media_duration_frames,
        clips=clips,
        audio_clips=audio_clips,
        identity_effect_counts=effect_counts,
        source_gap_frames=source_gaps,
    )


def probe_media(ffprobe: Path, media_path: Path) -> Dict[str, Any]:
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels,start_time,duration",
        "-of",
        "json",
        str(media_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if completed.returncode != 0:
        raise GateFailure(f"ffprobe failed: {completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    video = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise GateFailure("source media must contain both video and audio")
    duration_seconds = float(payload["format"]["duration"])
    rate_text = video.get("avg_frame_rate") or video.get("r_frame_rate")
    numerator, denominator = [int(part) for part in rate_text.split("/", 1)]
    return {
        "path": str(media_path.resolve()),
        "size_bytes": int(payload["format"]["size"]),
        "duration_seconds": duration_seconds,
        "duration_us": int(round(duration_seconds * US_PER_SECOND)),
        "video_codec": video.get("codec_name"),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps_num": numerator,
        "fps_den": denominator,
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": int(audio["sample_rate"]),
        "audio_channels": int(audio["channels"]),
    }


SRT_TIME_RE = re.compile(
    r"^(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3}) --> "
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})$"
)


def _srt_ms(match: re.Match[str], prefix: str) -> int:
    return (
        int(match.group(prefix + "h")) * 3_600_000
        + int(match.group(prefix + "m")) * 60_000
        + int(match.group(prefix + "s")) * 1_000
        + int(match.group(prefix + "ms"))
    )


def parse_srt(srt_path: Path) -> List[SrtCue]:
    text = srt_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    cues: List[SrtCue] = []
    for block_number, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.split("\n")]
        time_index = next((index for index, line in enumerate(lines) if SRT_TIME_RE.match(line)), None)
        if time_index is None:
            raise GateFailure(f"SRT block {block_number} has no valid timestamp range")
        match = SRT_TIME_RE.match(lines[time_index])
        assert match is not None
        cue_text = " ".join(line for line in lines[time_index + 1 :] if line).strip()
        if not cue_text:
            raise GateFailure(f"SRT block {block_number} has no subtitle text")
        cue_number = block_number
        if time_index > 0 and lines[0].isdigit():
            cue_number = int(lines[0])
        cues.append(
            SrtCue(
                index=cue_number,
                start_ms=_srt_ms(match, "s"),
                end_ms=_srt_ms(match, "e"),
                text=cue_text,
            )
        )
    if not cues:
        raise GateFailure("SRT contains no timestamp ranges")
    previous_end = 0
    for index, cue in enumerate(cues):
        if cue.start_ms < previous_end or cue.end_ms <= cue.start_ms:
            raise GateFailure(f"invalid SRT timing at cue {index + 1}")
        previous_end = cue.end_ms
    return cues


def validate_srt(srt_path: Path, expected_duration_us: int) -> Dict[str, Any]:
    cues = parse_srt(srt_path)
    expected_ms = expected_duration_us // 1000
    if abs(cues[-1].end_ms - expected_ms) > 1:
        raise GateFailure(f"SRT end {cues[-1].end_ms}ms does not match timeline {expected_ms}ms")
    return {
        "path": str(srt_path.resolve()),
        "cue_count": len(cues),
        "last_end_ms": cues[-1].end_ms,
    }


def validate_media_duration(
    xml_declared_frames: int,
    actual_frames: int,
    max_source_out: int,
) -> Dict[str, Any]:
    """Validate the complete-source duration without hiding material mismatches.

    Resolve can occasionally write an FCP7 file duration that omits one final,
    unused frame even though ffprobe measures that frame in the source file.  We
    accept only that exact, safe shape.  The opposite direction, a larger
    difference, or any clip using the extra frame remains a hard failure.
    """
    if xml_declared_frames <= 0 or actual_frames <= 0:
        raise GateFailure("source-media durations must be positive frame counts")
    if max_source_out < 0:
        raise GateFailure("maximum source out must be non-negative")
    if max_source_out > xml_declared_frames:
        raise GateFailure("at least one source range exceeds the XML-declared media duration")
    if max_source_out > actual_frames:
        raise GateFailure("at least one source range exceeds actual media duration")

    difference = actual_frames - xml_declared_frames
    if difference == 0:
        policy = "exact"
    elif difference == 1:
        policy = "one_unused_trailing_frame"
    else:
        raise GateFailure(
            f"XML media duration {xml_declared_frames} frames differs from actual {actual_frames} frames"
        )
    return {
        "policy": policy,
        "xml_declared_frames": xml_declared_frames,
        "actual_frames": actual_frames,
        "difference_frames": difference,
        "max_source_out": max_source_out,
    }


def analyze(
    xml_path: Path,
    ffprobe: Path,
    report_dir: Path,
    srt_path: Optional[Path] = None,
) -> Tuple[TimelineIR, Dict[str, Any], Dict[str, Any]]:
    report_dir.mkdir(parents=True, exist_ok=True)
    ir = parse_fcp7_xml(xml_path)
    media_path = Path(ir.media_path)
    if not media_path.is_file():
        raise GateFailure(f"source media does not exist: {media_path}")
    media = probe_media(ffprobe, media_path)
    if (media["fps_num"], media["fps_den"]) != (ir.fps, 1):
        raise GateFailure(
            f"media frame rate {media['fps_num']}/{media['fps_den']} does not match XML {ir.fps}/1"
        )
    actual_frames = int(round(media["duration_seconds"] * ir.fps))
    duration_compatibility = validate_media_duration(
        ir.xml_media_duration_frames,
        actual_frames,
        max(clip.source_out for clip in ir.clips),
    )
    media["duration_compatibility"] = duration_compatibility
    if (media["width"], media["height"]) != (ir.width, ir.height):
        raise GateFailure(
            f"media dimensions {media['width']}x{media['height']} do not match XML {ir.width}x{ir.height}"
        )

    srt = validate_srt(srt_path, ir.duration_us) if srt_path else None
    gap_frames = ir.source_gap_frames
    handles = {
        "first_left_frames": ir.clips[0].source_in,
        "last_right_frames": actual_frames - ir.clips[-1].source_out,
        "internal_gap_count": len(gap_frames),
        "internal_gap_min_frames": min(gap_frames) if gap_frames else 0,
        "internal_gap_max_frames": max(gap_frames) if gap_frames else 0,
        "internal_gap_total_frames": sum(gap_frames),
        "recoverable_total_seconds": (actual_frames - sum(clip.duration for clip in ir.clips)) / ir.fps,
    }
    gate_report = {
        "status": "passed",
        "input_xml": str(xml_path.resolve()),
        "gates": {
            "G-JY-01": {"passed": True, "fps": ir.fps, "canvas": [ir.width, ir.height], "tracks": [1, 1]},
            "G-JY-02": {"passed": True, "media_path": ir.media_path, "srt": srt},
            "G-JY-03": {"passed": True, "video_clips": len(ir.clips), "audio_clips": len(ir.audio_clips)},
            "G-JY-04": {
                "passed": True,
                "actual_media_frames": actual_frames,
                "max_source_out": max(c.source_out for c in ir.clips),
                "duration_compatibility": duration_compatibility,
            },
            "G-JY-05": {"passed": True, "duration_frames": ir.duration_frames, "gaps": 0, "overlaps": 0},
            "G-JY-06": {"passed": True, "identity_effect_counts": ir.identity_effect_counts, "speed": 1.0},
            "G-JY-07": {"passed": True, "note": "output collision is checked again during prepare/deploy"},
        },
        "handles": handles,
        "media": media,
    }
    _write_json_atomic(report_dir / "timeline_ir.json", ir.serializable())
    _write_json_atomic(report_dir / "gate_report.json", gate_report)
    return ir, media, gate_report


def _find_video_track(content: Dict[str, Any]) -> Tuple[int, Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]]]:
    materials = content.get("materials", {})
    videos = materials.get("videos", [])
    video_by_id = {item.get("id"): item for item in videos if isinstance(item, dict)}
    for track_index, track in enumerate(content.get("tracks", [])):
        segments = track.get("segments", [])
        for segment in segments:
            material = video_by_id.get(segment.get("material_id"))
            if material is None:
                continue
            refs = set(segment.get("extra_material_refs", []))
            aux: Dict[str, Dict[str, Any]] = {}
            for material_type, items in materials.items():
                if not isinstance(items, list):
                    continue
                match = next((item for item in items if isinstance(item, dict) and item.get("id") in refs), None)
                if match is not None:
                    aux[material_type] = match
            required = {
                "canvases",
                "placeholder_infos",
                "speeds",
                "sound_channel_mappings",
                "material_colors",
                "vocal_separations",
            }
            missing = sorted(required - set(aux))
            if missing:
                raise GateFailure(f"reference draft video segment lacks auxiliary materials: {missing}")
            return track_index, track, material, aux
    raise GateFailure("reference draft contains no identifiable video track")


def _build_caption_track(
    content: Dict[str, Any],
    cues: Sequence[SrtCue],
    group_id: str,
) -> Tuple[int, Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    materials = content.get("materials", {})
    text_materials = materials.get("texts", [])
    animation_materials = materials.get("material_animations", [])
    text_by_id = {
        item.get("id"): item for item in text_materials if isinstance(item, dict) and item.get("id")
    }
    animation_by_id = {
        item.get("id"): item
        for item in animation_materials
        if isinstance(item, dict) and item.get("id")
    }

    template_track_index: Optional[int] = None
    template_track: Optional[Dict[str, Any]] = None
    template_segment: Optional[Dict[str, Any]] = None
    template_text: Optional[Dict[str, Any]] = None
    template_animation: Optional[Dict[str, Any]] = None
    for track_index, track in enumerate(content.get("tracks", [])):
        for segment in track.get("segments", []):
            candidate_text = text_by_id.get(segment.get("material_id"))
            if candidate_text is None:
                continue
            animation = next(
                (
                    animation_by_id[ref]
                    for ref in segment.get("extra_material_refs", [])
                    if ref in animation_by_id
                ),
                None,
            )
            if animation is None:
                raise GateFailure("reference caption segment lacks a material animation")
            template_track_index = track_index
            template_track = track
            template_segment = segment
            template_text = candidate_text
            template_animation = animation
            break
        if template_track is not None:
            break
    if (
        template_track_index is None
        or template_track is None
        or template_segment is None
        or template_text is None
        or template_animation is None
    ):
        raise GateFailure("reference draft contains no identifiable caption track")

    new_segments: List[Dict[str, Any]] = []
    new_texts: List[Dict[str, Any]] = []
    new_animations: List[Dict[str, Any]] = []
    base_render_index = int(template_segment.get("render_index", 14000))
    for render_offset, cue in enumerate(cues):
        text_id = _uuid()
        animation_id = _uuid()

        text_material = copy.deepcopy(template_text)
        text_material["id"] = text_id
        text_material["group_id"] = group_id
        try:
            content_payload = json.loads(text_material["content"])
            styles = content_payload["styles"]
            if not isinstance(styles, list) or not styles:
                raise ValueError("empty styles")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GateFailure("reference caption has unsupported text content") from exc
        style = copy.deepcopy(styles[0])
        style["range"] = [0, _utf16_units(cue.text)]
        content_payload["styles"] = [style]
        content_payload["text"] = cue.text
        text_material["content"] = json.dumps(content_payload, ensure_ascii=False, separators=(",", ":"))
        new_texts.append(text_material)

        animation = copy.deepcopy(template_animation)
        animation["id"] = animation_id
        new_animations.append(animation)

        segment = copy.deepcopy(template_segment)
        segment["id"] = _uuid()
        segment["material_id"] = text_id
        segment["extra_material_refs"] = [animation_id]
        segment["render_index"] = base_render_index + render_offset
        segment["target_timerange"] = {"duration": (cue.end_ms - cue.start_ms) * 1000}
        if cue.start_ms:
            segment["target_timerange"]["start"] = cue.start_ms * 1000
        segment["render_timerange"] = {}
        new_segments.append(segment)

    new_track = copy.deepcopy(template_track)
    new_track["id"] = _uuid()
    new_track["segments"] = new_segments
    return template_track_index, new_track, new_texts, new_animations


def _copy_reference_sidecars(reference_dir: Path, staging_dir: Path, old_timeline_id: str, new_timeline_id: str) -> None:
    skip_dirs = {".backup"}
    skip_files = {
        "draft_content.json",
        "draft_content.json.bak",
        "draft_meta_info.json",
        "draft_cover.jpg",
        "project.json",
        "project.json.bak",
        "timeline_layout.json",
        "key_value.json",
        "draft_settings",
        "draft_virtual_store.json",
        "template-2.tmp",
        "template.tmp",
    }
    for source in reference_dir.rglob("*"):
        relative = source.relative_to(reference_dir)
        if any(part in skip_dirs for part in relative.parts):
            continue
        mapped_parts = [new_timeline_id if part == old_timeline_id else part for part in relative.parts]
        destination = staging_dir.joinpath(*mapped_parts)
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if source.name in skip_files:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _make_key_value(segment_ids: Sequence[str], media_name: str) -> Dict[str, Any]:
    analytics_material_id = uuid.uuid4().hex
    common = {
        "filter_category": "",
        "filter_detail": "",
        "is_brand": 0,
        "is_from_artist_shop": 0,
        "is_vip": "0",
        "keywordSource": "",
        "materialCategory": "media",
        "materialId": analytics_material_id,
        "materialName": media_name,
        "materialSubcategory": "local",
        "materialSubcategoryId": "",
        "materialThirdcategory": "导入",
        "materialThirdcategoryId": "",
        "material_copyright": "",
        "material_is_purchased": "",
        "rank": "0",
        "rec_id": "",
        "requestId": "",
        "role": "",
        "searchId": "",
        "searchKeyword": "",
        "team_id": "",
        "textTemplateVersion": "",
    }
    result: Dict[str, Any] = {}
    for segment_id in segment_ids:
        entry = dict(common)
        entry["segmentId"] = segment_id
        result[segment_id] = entry
    material_entry = dict(common)
    material_entry.update(
        {
            "commerce_template_cate": "",
            "commerce_template_pay_status": "",
            "commerce_template_pay_type": "",
            "douyin_music_is_avaliable": False,
            "enter_from": "",
            "is_favorite": False,
            "is_limited": False,
            "is_similar_music": False,
            "music_source": "",
            "previewed": 0,
            "previewed_before_added": 0,
            "template_need_purcahse": True,
        }
    )
    result[analytics_material_id] = material_entry
    return result


def _update_draft_materials(
    meta: Dict[str, Any],
    media_path: Path,
    media: Dict[str, Any],
    local_material_id: str,
    srt_path: Optional[Path],
    now_seconds: int,
    now_us: int,
) -> None:
    entries = meta.get("draft_materials", [])
    by_type = {int(item.get("type", -1)): item for item in entries if isinstance(item, dict)}
    for material_type in (0, 1, 2, 3, 6, 7, 8):
        by_type.setdefault(material_type, {"type": material_type, "value": []})
    by_type[0]["value"] = [
        {
            "ai_group_type": "",
            "create_time": int(media_path.stat().st_mtime),
            "duration": media["duration_us"],
            "enter_from": 0,
            "extra_info": media_path.name,
            "file_Path": _norm_jy_path(media_path),
            "height": media["height"],
            "id": local_material_id,
            "import_time": now_seconds,
            "import_time_ms": now_us,
            "item_source": 1,
            "material_color_tag": "",
            "md5": "",
            "metetype": "video",
            "roughcut_time_range": {"duration": media["duration_us"], "start": 0},
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0,
            "width": media["width"],
        }
    ]
    if srt_path is None:
        by_type[2]["value"] = []
    else:
        old_values = by_type[2].get("value", [])
        srt_id = old_values[0].get("id") if old_values else _uuid()
        by_type[2]["value"] = [
            {
                "ai_group_type": "",
                "create_time": 0,
                "duration": 0,
                "enter_from": 0,
                "extra_info": srt_path.name,
                "file_Path": _norm_jy_path(srt_path),
                "height": 0,
                "id": srt_id,
                "import_time": now_seconds,
                "import_time_ms": -1,
                "item_source": 1,
                "material_color_tag": "",
                "md5": "",
                "metetype": "none",
                "roughcut_time_range": {"duration": -1, "start": -1},
                "sub_time_range": {"duration": -1, "start": -1},
                "type": 2,
                "width": 0,
            }
        ]
    for material_type in (1, 3, 6, 7, 8):
        by_type[material_type]["value"] = []
    meta["draft_materials"] = [by_type[index] for index in (0, 1, 2, 3, 6, 7, 8)]


def _generate_cover(ffmpeg: Path, media_path: Path, source_frame: int, fps: int, output_path: Path) -> bool:
    source_seconds = source_frame / fps
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{source_seconds:.6f}",
        "-i",
        str(media_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-y",
        str(output_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return completed.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0


def prepare_draft(
    ir: TimelineIR,
    media: Dict[str, Any],
    report_dir: Path,
    prepared_root: Path,
    final_draft_root: Path,
    draft_name: str,
    reference_draft: Path,
    jianying_install_dir: Path,
    ffmpeg: Path,
    srt_path: Optional[Path],
) -> Path:
    prepared_root.mkdir(parents=True, exist_ok=True)
    final_target = (final_draft_root / draft_name).resolve()
    if final_target.parent != final_draft_root.resolve():
        raise GateFailure("draft name must resolve to a direct child of the draft root")
    if final_target.exists():
        raise FileExistsError(f"target draft already exists: {final_target}")

    output_dir = prepared_root / draft_name
    if output_dir.exists():
        raise FileExistsError(f"prepared draft already exists: {output_dir}")
    staging_dir = prepared_root / f"._prepare_{uuid.uuid4().hex}"

    codec = JianyingDraftCryptoCodec(
        DraftCryptoConfig(
            jy_install_dir=str(jianying_install_dir),
            isolated=True,
            validate_roundtrip=True,
            backup=False,
        )
    )
    reference_content = codec.decode((reference_draft / "draft_content.json").read_bytes())
    reference_meta = codec.decode((reference_draft / "draft_meta_info.json").read_bytes())
    project = _read_json(reference_draft / "Timelines" / "project.json")
    old_timeline_id = str(project["main_timeline_id"])
    old_video_track_index, old_video_track, base_video_material, aux_templates = _find_video_track(reference_content)

    timeline_id = _uuid()
    project_id = _uuid()
    draft_id = _uuid()
    local_material_id = str(uuid.uuid4())
    now_us = time.time_ns() // 1000
    now_seconds = now_us // US_PER_SECOND
    media_path = Path(ir.media_path)
    caption_cues = parse_srt(srt_path) if srt_path is not None else []

    content = copy.deepcopy(reference_content)
    content["id"] = timeline_id
    content["duration"] = ir.duration_us
    content["canvas_config"] = {"width": media["width"], "height": media["height"]}
    content["new_version"] = reference_content.get("new_version", "181.0.0")

    materials = content.setdefault("materials", {})
    for key in (
        "videos",
        "canvases",
        "placeholder_infos",
        "speeds",
        "sound_channel_mappings",
        "material_colors",
        "vocal_separations",
    ):
        materials[key] = []

    new_segments: List[Dict[str, Any]] = []
    segment_ids: List[str] = []
    for clip in ir.clips:
        material_id = _uuid()
        material = copy.deepcopy(base_video_material)
        material.update(
            {
                "id": material_id,
                "material_id": "",
                "local_material_id": local_material_id,
                "material_name": media_path.name,
                "path": _norm_jy_path(media_path),
                "duration": media["duration_us"],
                "width": media["width"],
                "height": media["height"],
                "type": "video",
                "category_name": "local",
            }
        )
        materials["videos"].append(material)

        extra_refs: List[str] = []
        for material_type in (
            "speeds",
            "placeholder_infos",
            "canvases",
            "sound_channel_mappings",
            "material_colors",
            "vocal_separations",
        ):
            aux = copy.deepcopy(aux_templates[material_type])
            aux["id"] = _uuid()
            materials[material_type].append(aux)
            extra_refs.append(aux["id"])

        segment = copy.deepcopy(old_video_track["segments"][0])
        segment_id = _uuid()
        segment["id"] = segment_id
        segment["material_id"] = material_id
        target_range = _trange(clip.target_start, clip.target_end, ir.fps)
        source_start_us = frame_to_us(clip.source_in, ir.fps)
        segment["source_timerange"] = {
            "start": source_start_us,
            "duration": target_range["duration"],
        }
        segment["target_timerange"] = target_range
        segment["extra_material_refs"] = extra_refs
        segment["render_timerange"] = {}
        new_segments.append(segment)
        segment_ids.append(segment_id)

    new_video_track = copy.deepcopy(old_video_track)
    new_video_track["id"] = _uuid()
    new_video_track["type"] = "mixed"
    new_video_track["is_default_name"] = True
    new_video_track["segments"] = new_segments
    content["tracks"][old_video_track_index] = new_video_track

    if srt_path is None:
        retained_tracks = [new_video_track]
        content["tracks"] = retained_tracks
        materials["texts"] = []
        materials["material_animations"] = []
    else:
        caption_track_index, caption_track, caption_texts, caption_animations = _build_caption_track(
            content,
            caption_cues,
            f"import_{now_us // 1000}",
        )
        if caption_track_index == old_video_track_index:
            raise GateFailure("reference video and caption tracks unexpectedly resolve to the same track")
        content["tracks"][caption_track_index] = caption_track
        materials["texts"] = caption_texts
        materials["material_animations"] = caption_animations

    meta = copy.deepcopy(reference_meta)
    meta.update(
        {
            "draft_id": draft_id,
            "draft_name": draft_name,
            "draft_fold_path": _norm_jy_path(final_target),
            "draft_root_path": _norm_jy_path(final_draft_root),
            "draft_cover": "draft_cover.jpg",
            "draft_timeline_materials_size_": media["size_bytes"] + (srt_path.stat().st_size if srt_path else 0),
            "tm_draft_create": now_us,
            "tm_draft_modified": now_us,
            "tm_draft_removed": 0,
            "tm_duration": ir.duration_us,
        }
    )
    _update_draft_materials(meta, media_path, media, local_material_id, srt_path, now_seconds, now_us)

    project = {
        "config": copy.deepcopy(project.get("config", {})),
        "create_time": now_us,
        "id": project_id,
        "main_timeline_id": timeline_id,
        "timelines": [
            {
                "create_time": now_us,
                "id": timeline_id,
                "is_marked_delete": False,
                "name": "时间线01",
                "update_time": now_us,
            }
        ],
        "update_time": now_us,
        "version": 0,
    }
    layout = {
        "activeTimeline": timeline_id,
        "dockItems": [
            {"dockIndex": 0, "ratio": 1, "timelineIds": [timeline_id], "timelineNames": ["时间线01"]}
        ],
        "layoutOrientation": 1,
    }
    settings = (
        "[General]\n"
        f"draft_create_time={now_seconds}\n"
        f"draft_last_edit_time={now_seconds}\n"
        "real_edit_seconds=0\n"
        "real_edit_keys=0\n"
        "cloud_last_modify_platform=windows\n"
    )
    virtual_store = {
        "draft_materials": [],
        "draft_virtual_store": [
            {
                "type": 0,
                "value": [
                    {
                        "creation_time": 0,
                        "display_name": "",
                        "filter_type": 0,
                        "id": "",
                        "import_time": 0,
                        "import_time_us": 0,
                        "material_color_tag": "",
                        "sort_sub_type": 0,
                        "sort_type": 0,
                        "subdraft_filter_type": 0,
                    }
                ],
            },
            {"type": 1, "value": [{"child_id": local_material_id, "parent_id": ""}]},
            {"type": 2, "value": []},
        ],
    }
    source_xml_path = Path(ir.xml_path)
    provenance = {
        "schema": "io.github.jianying-timeline-bridge.provenance",
        "schema_version": 1,
        "bridge_version": BRIDGE_VERSION,
        "created_at_us": now_us,
        "conversion": "fcp7_xml_to_jianying",
        "draft": {
            "draft_id": draft_id,
            "timeline_id": timeline_id,
            "project_id": project_id,
            "draft_name": draft_name,
            "intended_target": str(final_target),
        },
        "compatibility": {
            "jianying_build": jianying_install_dir.name,
            "videoeditor_dll_sha256": _sha256(jianying_install_dir / "videoeditor.dll"),
            "encrypted_content": True,
        },
        "source": {
            "xml": {
                "path": str(source_xml_path.resolve()),
                "sha256": _sha256(source_xml_path),
            },
            "srt": (
                {"path": str(srt_path.resolve()), "sha256": _sha256(srt_path)}
                if srt_path is not None
                else None
            ),
            "media": {
                "path": str(media_path.resolve()),
                "sampled_fingerprint": sampled_media_fingerprint(media_path),
                "duration_us": media["duration_us"],
                "width": media["width"],
                "height": media["height"],
                "fps_num": media["fps_num"],
                "fps_den": media["fps_den"],
            },
        },
        "timeline": {
            "sequence_name": ir.sequence_name,
            "fps": ir.fps,
            "ntsc": ir.ntsc,
            "width": ir.width,
            "height": ir.height,
            "duration_frames": ir.duration_frames,
            "video_segment_count": len(new_segments),
            "caption_segment_count": len(caption_cues),
        },
        "segment_map": [
            {
                "segment_id": segment_id,
                "source_clip_id": clip.clip_id,
                "clip_index": clip.index,
                "target_start_frame": clip.target_start,
                "target_end_frame": clip.target_end,
                "source_in_frame": clip.source_in,
                "source_out_frame": clip.source_out,
            }
            for clip, segment_id in zip(ir.clips, segment_ids)
        ],
    }

    try:
        _copy_reference_sidecars(reference_draft, staging_dir, old_timeline_id, timeline_id)
        timeline_dir = staging_dir / "Timelines" / timeline_id
        timeline_dir.mkdir(parents=True, exist_ok=True)

        content_text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        meta_text = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        encrypted_content = codec.encode(content_text)
        encrypted_meta = codec.encode(meta_text)
        if codec.decode(encrypted_content) != content:
            raise GateFailure("encrypted content round-trip differs")
        if codec.decode(encrypted_meta) != meta:
            raise GateFailure("encrypted meta round-trip differs")

        for path in (
            staging_dir / "draft_content.json",
            staging_dir / "draft_content.json.bak",
            timeline_dir / "draft_content.json",
            timeline_dir / "draft_content.json.bak",
        ):
            _write_bytes_atomic(path, encrypted_content)
        _write_bytes_atomic(staging_dir / "draft_meta_info.json", encrypted_meta)
        _write_json_atomic(staging_dir / "Timelines" / "project.json", project, compact=True)
        _write_json_atomic(staging_dir / "Timelines" / "project.json.bak", project, compact=True)
        _write_json_atomic(staging_dir / "timeline_layout.json", layout, compact=True)
        _write_json_atomic(staging_dir / "key_value.json", _make_key_value(segment_ids, media_path.name), compact=True)
        _write_json_atomic(staging_dir / "draft_virtual_store.json", virtual_store, compact=True)
        _write_json_atomic(staging_dir / PROVENANCE_FILENAME, provenance)
        _write_bytes_atomic(staging_dir / "draft_settings", settings.encode("utf-8"))

        cover_path = staging_dir / "draft_cover.jpg"
        if _generate_cover(ffmpeg, media_path, ir.clips[0].source_in, ir.fps, cover_path):
            shutil.copy2(cover_path, timeline_dir / "draft_cover.jpg")

        os.replace(staging_dir, output_dir)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    expected_caption_segments = len(caption_cues)
    manifest = {
        "schema_version": 1,
        "prepared_at_us": now_us,
        "draft_name": draft_name,
        "prepared_path": str(output_dir.resolve()),
        "intended_target": str(final_target),
        "draft_root": str(final_draft_root.resolve()),
        "reference_draft": str(reference_draft.resolve()),
        "source_xml": ir.xml_path,
        "source_media": ir.media_path,
        "source_srt": str(srt_path.resolve()) if srt_path else None,
        "draft_id": draft_id,
        "timeline_id": timeline_id,
        "project_id": project_id,
        "fps": ir.fps,
        "duration_us": ir.duration_us,
        "video_segments": len(new_segments),
        "caption_segments": expected_caption_segments,
        "caption_last_end_us": caption_cues[-1].end_ms * 1000 if caption_cues else 0,
        "media_materials": len(materials["videos"]),
        "source_media_duration_us": media["duration_us"],
        "bridge_version": BRIDGE_VERSION,
        "provenance_file": PROVENANCE_FILENAME,
        "provenance_sha256": _sha256(output_dir / PROVENANCE_FILENAME),
        "content_sha256": _sha256(output_dir / "draft_content.json"),
        "meta_sha256": _sha256(output_dir / "draft_meta_info.json"),
        "root_index_entry": {
            "cloud_draft_cover": False,
            "cloud_draft_sync": False,
            "draft_cloud_last_action_download": False,
            "draft_cloud_purchase_info": "",
            "draft_cloud_template_id": "",
            "draft_cloud_tutorial_info": "",
            "draft_cloud_videocut_purchase_info": "",
            "draft_cover": _norm_jy_path(final_target / "draft_cover.jpg"),
            "draft_fold_path": _norm_jy_path(final_target),
            "draft_id": draft_id,
            "draft_is_ai_shorts": False,
            "draft_is_cloud_temp_draft": False,
            "draft_is_invisible": False,
            "draft_is_pippit_draft": False,
            "draft_is_web_article_video": False,
            "draft_json_file": _norm_jy_path(final_target / "draft_content.json"),
            "draft_name": draft_name,
            "draft_new_version": "",
            "draft_root_path": _norm_jy_path(final_draft_root),
            "draft_timeline_materials_size": media["size_bytes"] + (srt_path.stat().st_size if srt_path else 0),
            "draft_type": "",
            "draft_web_article_video_enter_from": "",
            "pippit_avatar_url": "",
            "pippit_extra_info": "",
            "pippit_id": "",
            "pippit_user_name": "",
            "streaming_edit_draft_ready": True,
            "tm_draft_cloud_completed": "",
            "tm_draft_cloud_entry_id": -1,
            "tm_draft_cloud_modified": 0,
            "tm_draft_cloud_parent_entry_id": -1,
            "tm_draft_cloud_space_id": -1,
            "tm_draft_cloud_user_id": -1,
            "tm_draft_create": now_us,
            "tm_draft_modified": now_us,
            "tm_draft_removed": 0,
            "tm_duration": ir.duration_us,
        },
    }
    _write_json_atomic(report_dir / "prepared_manifest.json", manifest)
    return output_dir


def _jianying_is_running() -> bool:
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return "JianyingPro.exe".lower() in completed.stdout.lower()


def _validate_provenance_against_manifest(
    provenance: Dict[str, Any], manifest: Dict[str, Any]
) -> None:
    if provenance.get("schema") != "io.github.jianying-timeline-bridge.provenance":
        raise GateFailure("prepared provenance schema is unsupported")
    if provenance.get("schema_version") != 1:
        raise GateFailure("prepared provenance schema version is unsupported")
    draft_info = provenance.get("draft")
    if not isinstance(draft_info, dict):
        raise GateFailure("prepared provenance draft section is missing")
    for key in ("draft_id", "timeline_id", "project_id", "draft_name"):
        if draft_info.get(key) != manifest.get(key):
            raise GateFailure(f"prepared provenance draft.{key} differs from manifest")
    compatibility = provenance.get("compatibility")
    if (
        not isinstance(compatibility, dict)
        or not isinstance(compatibility.get("jianying_build"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(compatibility.get("videoeditor_dll_sha256", "")))
    ):
        raise GateFailure("prepared provenance compatibility fingerprint is missing")
    source = provenance.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("media"), dict):
        raise GateFailure("prepared provenance source media is missing")
    if Path(str(source["media"].get("path", ""))).resolve() != Path(manifest["source_media"]).resolve():
        raise GateFailure("prepared provenance source media differs from manifest")
    timeline = provenance.get("timeline")
    if not isinstance(timeline, dict):
        raise GateFailure("prepared provenance timeline section is missing")
    expected_timeline = {
        "fps": manifest["fps"],
        "duration_frames": us_to_frame(int(manifest["duration_us"]), int(manifest["fps"])),
        "video_segment_count": manifest["video_segments"],
        "caption_segment_count": manifest.get("caption_segments", 0),
    }
    for key, value in expected_timeline.items():
        if timeline.get(key) != value:
            raise GateFailure(f"prepared provenance timeline.{key} differs from manifest")
    segment_map = provenance.get("segment_map")
    if not isinstance(segment_map, list) or len(segment_map) != manifest["video_segments"]:
        raise GateFailure("prepared provenance segment map count differs from manifest")
    segment_ids: List[str] = []
    previous_target_end = 0
    integer_fields = (
        "clip_index",
        "target_start_frame",
        "target_end_frame",
        "source_in_frame",
        "source_out_frame",
    )
    for index, entry in enumerate(segment_map):
        if not isinstance(entry, dict):
            raise GateFailure(f"prepared provenance segment map is invalid at index {index}")
        segment_id = entry.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id:
            raise GateFailure(
                f"prepared provenance segment map contains an invalid segment id at index {index}"
            )
        segment_ids.append(segment_id)
        for field in integer_fields:
            value = entry.get(field)
            if type(value) is not int or value < 0:
                raise GateFailure(
                    f"prepared provenance segment_map[{index}].{field} must be a non-negative integer"
                )
        if entry["clip_index"] != index:
            raise GateFailure(
                f"prepared provenance segment_map[{index}].clip_index is not canonical"
            )
        target_start = entry["target_start_frame"]
        target_end = entry["target_end_frame"]
        source_in = entry["source_in_frame"]
        source_out = entry["source_out_frame"]
        if target_start != previous_target_end or target_end <= target_start:
            raise GateFailure(
                f"prepared provenance segment map is not a continuous positive timeline at index {index}"
            )
        if source_out <= source_in or source_out - source_in != target_end - target_start:
            raise GateFailure(
                f"prepared provenance source/target duration differs at index {index}"
            )
        previous_target_end = target_end
    if len(set(segment_ids)) != len(segment_ids):
        raise GateFailure("prepared provenance segment map contains duplicate segment ids")
    expected_duration_frames = us_to_frame(int(manifest["duration_us"]), int(manifest["fps"]))
    if previous_target_end != expected_duration_frames:
        raise GateFailure("prepared provenance segment map duration differs from manifest")


def _validated_sidecar_name(value: Any) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise GateFailure("provenance filename must be a safe direct-child basename")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise GateFailure("provenance filename must be a safe direct-child basename")
    return value


def _verify_manifest_files(prepared: Path, manifest: Dict[str, Any]) -> None:
    if _sha256(prepared / "draft_content.json") != manifest["content_sha256"]:
        raise GateFailure("prepared draft_content.json hash differs from manifest")
    if _sha256(prepared / "draft_meta_info.json") != manifest["meta_sha256"]:
        raise GateFailure("prepared draft_meta_info.json hash differs from manifest")
    provenance_filename = manifest.get("provenance_file")
    if not provenance_filename:
        raise GateFailure("prepared manifest does not declare timeline bridge provenance")
    provenance_filename = _validated_sidecar_name(provenance_filename)
    provenance_path = prepared / provenance_filename
    if not provenance_path.is_file():
        raise GateFailure("prepared timeline bridge provenance is missing")
    if _sha256(provenance_path) != manifest.get("provenance_sha256"):
        raise GateFailure("prepared timeline bridge provenance hash differs from manifest")
    _validate_provenance_against_manifest(_read_json(provenance_path), manifest)


def _deployment_journal_path(draft_root: Path, draft_id: str) -> Path:
    if not re.fullmatch(r"[0-9A-Fa-f-]{36}", draft_id):
        raise GateFailure("draft id is unsafe for a deployment journal filename")
    return draft_root / f"._timeline_bridge_deploy_{draft_id}.json"


def _update_deployment_journal(
    path: Path,
    payload: Dict[str, Any],
    state: str,
    *,
    error: Optional[str] = None,
) -> None:
    payload["state"] = state
    payload["updated_at_us"] = time.time_ns() // 1000
    if error is not None:
        payload["error"] = error
    _write_json_atomic(path, payload)


def _archive_deployment_journal(journal_path: Path, report_dir: Path, suffix: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    archived = report_dir / f"deployment_journal.{suffix}.json"
    if archived.exists():
        if _sha256(archived) == _sha256(journal_path):
            journal_path.unlink()
            return archived
        archived = report_dir / f"deployment_journal.{suffix}.{time.time_ns() // 1000}.json"
        if archived.exists():
            raise FileExistsError(f"deployment journal archive collision: {archived}")
    shutil.copy2(journal_path, archived)
    journal_path.unlink()
    return archived


def _validate_root_index_entry(
    entry: Any,
    *,
    manifest: Dict[str, Any],
    target: Path,
    draft_root: Path,
) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        raise GateFailure("manifest root_index_entry is not an object")
    expected = {
        "draft_id": manifest["draft_id"],
        "draft_name": manifest["draft_name"],
        "draft_fold_path": _norm_jy_path(target),
        "draft_json_file": _norm_jy_path(target / "draft_content.json"),
        "draft_root_path": _norm_jy_path(draft_root),
    }
    for key, expected_value in expected.items():
        actual = entry.get(key)
        if key.endswith("path") or key == "draft_json_file":
            if str(actual).replace("\\", "/").casefold() != str(expected_value).casefold():
                raise GateFailure(f"manifest root_index_entry.{key} differs from the guarded target")
        elif actual != expected_value:
            raise GateFailure(f"manifest root_index_entry.{key} differs from the guarded target")
    expected_entry = manifest.get("root_index_entry")
    if expected_entry is not None:
        if not isinstance(expected_entry, dict):
            raise GateFailure("manifest root_index_entry is not an object")
        if entry != expected_entry:
            raise GateFailure("registered root index entry differs from the committed manifest")
    return entry


def deploy(prepared: Path, manifest_path: Path, root_meta_info: Path, report_dir: Path) -> Path:
    if _jianying_is_running():
        raise GateFailure("JianyingPro is running; close it before deployment")
    manifest = _read_json(manifest_path)
    _verify_manifest_files(prepared, manifest)
    target = Path(manifest["intended_target"]).resolve()
    draft_root = Path(manifest["draft_root"]).resolve()
    if target.parent != draft_root:
        raise GateFailure("manifest target is not a direct child of draft root")
    if target.exists():
        raise FileExistsError(f"target draft already exists: {target}")
    if prepared.name != manifest["draft_name"]:
        raise GateFailure("prepared directory name differs from manifest")
    validated_root_entry = _validate_root_index_entry(
        manifest.get("root_index_entry"), manifest=manifest, target=target, draft_root=draft_root
    )
    draft_id = manifest["draft_id"]
    journal_path = _deployment_journal_path(draft_root, draft_id)
    if journal_path.exists():
        raise FileExistsError(
            f"unfinished deployment journal exists; run recovery before retrying: {journal_path}"
        )

    root_payload = _read_json(root_meta_info)
    root_hash_before = _sha256(root_meta_info)
    entries = root_payload.setdefault("all_draft_store", [])
    if not isinstance(entries, list):
        raise GateFailure("root_meta_info all_draft_store is not a list")
    target_norm = _norm_jy_path(target).casefold()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("draft_id", "")) == draft_id:
            raise GateFailure("draft id already exists in root metadata")
        entry_path = str(entry.get("draft_fold_path", "")).replace("\\", "/").casefold()
        if entry_path == target_norm:
            raise GateFailure("draft path already exists in root metadata")

    report_dir.mkdir(parents=True, exist_ok=True)
    backup = report_dir / f"root_meta_info.before_{manifest['draft_name']}.json"
    if backup.exists():
        raise FileExistsError(f"root metadata backup already exists: {backup}")
    shutil.copy2(root_meta_info, backup)

    staging = draft_root / f"._codex_build_{uuid.uuid4().hex}"
    if staging.exists():
        raise FileExistsError(f"unexpected staging collision: {staging}")
    entries.insert(0, validated_root_entry)
    journal = {
        "schema": "io.github.jianying-timeline-bridge.deployment-journal",
        "schema_version": 1,
        "bridge_version": manifest.get("bridge_version", "0.1.1-alpha.1"),
        "draft_id": draft_id,
        "draft_name": manifest["draft_name"],
        "timeline_id": manifest["timeline_id"],
        "project_id": manifest["project_id"],
        "fps": manifest["fps"],
        "duration_us": manifest["duration_us"],
        "video_segments": manifest["video_segments"],
        "caption_segments": manifest.get("caption_segments", 0),
        "source_media": manifest["source_media"],
        "prepared": str(prepared.resolve()),
        "manifest": str(manifest_path.resolve()),
        "staging": str(staging.resolve()),
        "target": str(target),
        "draft_root": str(draft_root),
        "root_meta_info": str(root_meta_info.resolve()),
        "root_meta_backup": str(backup.resolve()),
        "root_meta_sha256_before": root_hash_before,
        "content_sha256": manifest["content_sha256"],
        "meta_sha256": manifest["meta_sha256"],
        "provenance_file": manifest.get("provenance_file"),
        "provenance_sha256": manifest.get("provenance_sha256"),
        "root_index_entry": validated_root_entry,
        "created_at_us": time.time_ns() // 1000,
    }
    _update_deployment_journal(journal_path, journal, "PREPARED")
    target_committed = False
    root_commit_attempted = False
    try:
        shutil.copytree(prepared, staging)
        _verify_manifest_files(staging, manifest)
        _update_deployment_journal(journal_path, journal, "TARGET_STAGED")
        if target.exists():
            raise FileExistsError(f"target draft appeared during deployment: {target}")
        if _sha256(root_meta_info) != root_hash_before:
            raise GateFailure("root metadata changed during deployment; retry after closing all writers")

        os.replace(staging, target)
        target_committed = True
        _update_deployment_journal(journal_path, journal, "TARGET_COMMITTED")
        root_commit_attempted = True
        _write_json_atomic(root_meta_info, root_payload, compact=True)
        committed_root = _read_json(root_meta_info)
        if not any(
            isinstance(entry, dict) and entry.get("draft_id") == draft_id
            for entry in committed_root.get("all_draft_store", [])
        ):
            raise GateFailure("root metadata write did not preserve the new draft entry")
        _update_deployment_journal(journal_path, journal, "INDEX_COMMITTED")
    except Exception as exc:
        if root_commit_attempted:
            _write_bytes_atomic(root_meta_info, backup.read_bytes())
        if target_committed and target.exists() and not staging.exists():
            os.replace(target, staging)
        if staging.exists():
            shutil.rmtree(staging)
        _update_deployment_journal(journal_path, journal, "ROLLED_BACK", error=str(exc))
        _archive_deployment_journal(journal_path, report_dir, "rolled_back")
        raise

    deployment = {
        "status": "deployed",
        "target": str(target),
        "root_meta_info": str(root_meta_info),
        "root_meta_backup": str(backup),
        "deployment_journal": str(journal_path),
        "journal_state": "INDEX_COMMITTED",
        "draft_id": draft_id,
        "timestamp_us": time.time_ns() // 1000,
    }
    _write_json_atomic(report_dir / "deployment_report.json", deployment)
    return target


def recover_deployments(draft_root: Path, root_meta_info: Path, report_dir: Path) -> Dict[str, Any]:
    """Resolve only transactions created by this bridge; never touch unrelated drafts."""

    if _jianying_is_running():
        raise GateFailure("JianyingPro is running; close it before deployment recovery")
    draft_root = draft_root.resolve()
    root_meta_info = root_meta_info.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    recovery_report_path = report_dir / "recovery_report.json"
    if recovery_report_path.exists():
        raise FileExistsError(f"recovery report already exists: {recovery_report_path}")
    actions: List[Dict[str, Any]] = []

    for journal_path in sorted(draft_root.glob("._timeline_bridge_deploy_*.json")):
        journal = _read_json(journal_path)
        draft_id = str(journal.get("draft_id", ""))
        target = Path(str(journal.get("target", ""))).resolve()
        staging = Path(str(journal.get("staging", ""))).resolve()
        if target.parent != draft_root or staging.parent != draft_root:
            raise GateFailure(f"recovery journal contains a path outside the draft root: {journal_path}")
        if Path(str(journal.get("root_meta_info", ""))).resolve() != root_meta_info:
            raise GateFailure(f"recovery journal points to a different root metadata file: {journal_path}")

        def directory_matches(candidate: Path) -> bool:
            try:
                provenance_file = _validated_sidecar_name(journal.get("provenance_file"))
            except GateFailure:
                return False
            provenance_sha256 = journal.get("provenance_sha256")
            basic_match = (
                candidate.is_dir()
                and (candidate / "draft_content.json").is_file()
                and (candidate / "draft_meta_info.json").is_file()
                and (candidate / "Timelines" / "project.json").is_file()
                and (
                    candidate
                    / "Timelines"
                    / str(journal.get("timeline_id", ""))
                    / "draft_content.json"
                ).is_file()
                and isinstance(provenance_sha256, str)
                and (candidate / provenance_file).is_file()
                and _sha256(candidate / "draft_content.json") == journal.get("content_sha256")
                and _sha256(candidate / "draft_meta_info.json") == journal.get("meta_sha256")
                and _sha256(candidate / provenance_file) == provenance_sha256
            )
            if not basic_match:
                return False
            try:
                project = _read_json(candidate / "Timelines" / "project.json")
                if project.get("main_timeline_id") != journal.get("timeline_id"):
                    return False
                timeline_content_path = (
                    candidate
                    / "Timelines"
                    / str(journal.get("timeline_id"))
                    / "draft_content.json"
                )
                if _sha256(timeline_content_path) != journal.get("content_sha256"):
                    return False
                _validate_provenance_against_manifest(
                    _read_json(candidate / str(provenance_file)), journal
                )
            except (GateFailure, OSError, ValueError, json.JSONDecodeError):
                return False
            return True

        root_payload = _read_json(root_meta_info)
        entries = root_payload.get("all_draft_store", [])
        if not isinstance(entries, list):
            raise GateFailure("root_meta_info all_draft_store is not a list")
        matching_entries = [
            entry for entry in entries if isinstance(entry, dict) and entry.get("draft_id") == draft_id
        ]
        if len(matching_entries) > 1:
            raise GateFailure(f"duplicate draft id entries block recovery: {journal_path}")
        target_norm = _norm_jy_path(target).casefold()
        registered = False
        if matching_entries:
            registered_path = str(matching_entries[0].get("draft_fold_path", "")).replace(
                "\\", "/"
            ).casefold()
            if registered_path != target_norm:
                raise GateFailure(f"draft id is registered to a different path: {journal_path}")
            _validate_root_index_entry(
                matching_entries[0],
                manifest=journal,
                target=target,
                draft_root=draft_root,
            )
            registered = True

        stored_entry = _validate_root_index_entry(
            journal.get("root_index_entry"),
            manifest=journal,
            target=target,
            draft_root=draft_root,
        )

        quarantines: List[str] = []

        def quarantine_candidate(candidate: Path) -> None:
            quarantine = draft_root / (
                f"._timeline_bridge_quarantine_{draft_id}_{time.time_ns() // 1000}"
            )
            if quarantine.exists():
                raise FileExistsError(f"unexpected quarantine collision: {quarantine}")
            os.replace(candidate, quarantine)
            quarantines.append(str(quarantine))

        terminal_rollback = journal.get("state") in {"ROLLED_BACK", "RECOVERED_QUARANTINED"}
        target_good = False if terminal_rollback else directory_matches(target)
        staging_good = False if terminal_rollback else directory_matches(staging)
        if target.exists() and not target_good:
            quarantine_candidate(target)
            target_good = False
        if not target.exists() and staging.exists() and staging_good:
            os.replace(staging, target)
            target_good = True
            staging_good = False
        if staging.exists():
            quarantine_candidate(staging)

        if target.exists() and target_good:
            if not registered:
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    entry_path = str(entry.get("draft_fold_path", "")).replace("\\", "/").casefold()
                    if entry_path == target_norm or entry.get("draft_id") == draft_id:
                        raise GateFailure(f"root metadata collision blocks recovery: {journal_path}")
                recovery_backup = report_dir / f"root_meta_info.recovery_before_{draft_id}.json"
                if recovery_backup.exists():
                    recovery_backup = report_dir / (
                        f"root_meta_info.recovery_before_{draft_id}.{time.time_ns() // 1000}.json"
                    )
                shutil.copy2(root_meta_info, recovery_backup)
                entries.insert(0, stored_entry)
                _write_json_atomic(root_meta_info, root_payload, compact=True)
                registered = True
            _update_deployment_journal(journal_path, journal, "RECOVERED_VERIFIED")
            archive = _archive_deployment_journal(
                journal_path, report_dir, f"recovered_{draft_id}"
            )
            actions.append(
                {
                    "draft_id": draft_id,
                    "action": "completed",
                    "target": str(target),
                    "quarantine": quarantines,
                    "journal_archive": str(archive),
                }
            )
            continue

        # A created directory whose hashes are no longer trusted is preserved in
        # quarantine for diagnosis, while its official target name is released.
        if target.exists():
            quarantine_candidate(target)

        if registered:
            recovery_backup = report_dir / f"root_meta_info.recovery_before_{draft_id}.json"
            if recovery_backup.exists():
                recovery_backup = report_dir / (
                    f"root_meta_info.recovery_before_{draft_id}.{time.time_ns() // 1000}.json"
                )
            shutil.copy2(root_meta_info, recovery_backup)
            root_payload["all_draft_store"] = [
                entry
                for entry in entries
                if not (isinstance(entry, dict) and entry.get("draft_id") == draft_id)
            ]
            _write_json_atomic(root_meta_info, root_payload, compact=True)

        _update_deployment_journal(journal_path, journal, "RECOVERED_QUARANTINED")
        archive = _archive_deployment_journal(
            journal_path, report_dir, f"quarantined_{draft_id}"
        )
        actions.append(
            {
                "draft_id": draft_id,
                "action": "quarantined" if quarantines else "rolled_back",
                "quarantine": quarantines,
                "journal_archive": str(archive),
            }
        )

    result = {"status": "passed", "recovered_transactions": len(actions), "actions": actions}
    _write_json_atomic(recovery_report_path, result)
    return result


def _validate_generated_provenance(
    provenance: Dict[str, Any],
    *,
    manifest: Dict[str, Any],
    content: Dict[str, Any],
    meta: Dict[str, Any],
    jianying_install_dir: Path,
    video_segments: Sequence[Dict[str, Any]],
) -> None:
    if provenance.get("schema") != "io.github.jianying-timeline-bridge.provenance":
        raise GateFailure("timeline bridge provenance schema is unsupported")
    if provenance.get("schema_version") != 1:
        raise GateFailure("timeline bridge provenance schema version is unsupported")
    draft_info = provenance.get("draft")
    if not isinstance(draft_info, dict):
        raise GateFailure("timeline bridge provenance draft section is missing")
    expected_draft = {
        "draft_id": manifest["draft_id"],
        "timeline_id": manifest["timeline_id"],
        "project_id": manifest["project_id"],
        "draft_name": manifest["draft_name"],
    }
    for key, value in expected_draft.items():
        if draft_info.get(key) != value:
            raise GateFailure(f"timeline bridge provenance draft.{key} differs from manifest")
    if content.get("id") != draft_info["timeline_id"] or meta.get("draft_id") != draft_info["draft_id"]:
        raise GateFailure("timeline bridge provenance IDs differ from encrypted draft data")

    compatibility = provenance.get("compatibility")
    if not isinstance(compatibility, dict):
        raise GateFailure("timeline bridge provenance compatibility section is missing")
    if compatibility.get("jianying_build") != jianying_install_dir.name:
        raise GateFailure("timeline bridge provenance Jianying build differs")
    dll_path = jianying_install_dir / "videoeditor.dll"
    if compatibility.get("videoeditor_dll_sha256") != _sha256(dll_path):
        raise GateFailure("timeline bridge provenance DLL fingerprint differs")

    source = provenance.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("media"), dict):
        raise GateFailure("timeline bridge provenance source media is missing")
    source_media = source["media"]
    if Path(str(source_media.get("path", ""))).resolve() != Path(manifest["source_media"]).resolve():
        raise GateFailure("timeline bridge provenance source media path differs")
    expected_fingerprint = source_media.get("sampled_fingerprint")
    if not isinstance(expected_fingerprint, dict):
        raise GateFailure("timeline bridge provenance media fingerprint is missing")
    actual_fingerprint = sampled_media_fingerprint(Path(manifest["source_media"]))
    if (
        actual_fingerprint.get("algorithm") != expected_fingerprint.get("algorithm")
        or actual_fingerprint.get("digest") != expected_fingerprint.get("digest")
        or actual_fingerprint.get("size_bytes") != expected_fingerprint.get("size_bytes")
    ):
        raise GateFailure("timeline bridge provenance media fingerprint differs")

    segment_map = provenance.get("segment_map")
    if not isinstance(segment_map, list) or len(segment_map) != len(video_segments):
        raise GateFailure("timeline bridge provenance segment map count differs")
    fps = int(manifest["fps"])
    mapped_ids: List[str] = []
    for index, (entry, segment) in enumerate(zip(segment_map, video_segments)):
        if not isinstance(entry, dict) or not isinstance(entry.get("segment_id"), str):
            raise GateFailure(f"timeline bridge provenance segment map is invalid at index {index}")
        mapped_ids.append(entry["segment_id"])
        if entry.get("clip_index") != index:
            raise GateFailure(
                f"timeline bridge provenance segment_map[{index}].clip_index differs"
            )
        target_range = segment.get("target_timerange")
        source_range = segment.get("source_timerange")
        if not isinstance(target_range, dict) or not isinstance(source_range, dict):
            raise GateFailure(f"generated segment timerange is missing at index {index}")
        target_start_us = int(target_range.get("start", 0))
        target_duration_us = int(target_range.get("duration", 0))
        source_start_us = int(source_range.get("start", 0))
        source_duration_us = int(source_range.get("duration", 0))
        actual_frames = {
            "target_start_frame": checked_us_to_frame(
                target_start_us, fps, f"segment[{index}].target.start"
            ),
            "target_end_frame": checked_us_to_frame(
                target_start_us + target_duration_us,
                fps,
                f"segment[{index}].target.end",
            ),
            "source_in_frame": checked_us_to_frame(
                source_start_us, fps, f"segment[{index}].source.start"
            ),
            "source_out_frame": checked_us_to_frame(
                source_start_us + source_duration_us,
                fps,
                f"segment[{index}].source.end",
            ),
        }
        for field, actual in actual_frames.items():
            expected = entry.get(field)
            if type(expected) is not int or expected < 0:
                raise GateFailure(
                    f"timeline bridge provenance segment_map[{index}].{field} is invalid"
                )
            if expected != actual:
                raise GateFailure(
                    f"timeline bridge provenance segment_map[{index}].{field} differs from encrypted draft"
                )
    video_segment_ids = [str(segment.get("id", "")) for segment in video_segments]
    if len(set(mapped_ids)) != len(mapped_ids) or mapped_ids != video_segment_ids:
        raise GateFailure("timeline bridge provenance segment IDs differ from generated segments")


def verify_draft(
    draft_path: Path,
    manifest_path: Path,
    jianying_install_dir: Path,
    root_meta_info: Optional[Path],
    report_dir: Path,
) -> Dict[str, Any]:
    manifest = _read_json(manifest_path)
    codec = JianyingDraftCryptoCodec(
        DraftCryptoConfig(
            jy_install_dir=str(jianying_install_dir),
            isolated=True,
            validate_roundtrip=True,
            backup=False,
        )
    )
    content = codec.decode((draft_path / "draft_content.json").read_bytes())
    meta = codec.decode((draft_path / "draft_meta_info.json").read_bytes())
    project = _read_json(draft_path / "Timelines" / "project.json")
    timeline_id = project["main_timeline_id"]
    timeline_content_path = draft_path / "Timelines" / timeline_id / "draft_content.json"
    timeline_content = codec.decode(timeline_content_path.read_bytes())
    if content != timeline_content:
        raise GateFailure("root and timeline draft_content differ")
    if content.get("id") != manifest["timeline_id"]:
        raise GateFailure("timeline id differs from manifest")
    if meta.get("draft_id") != manifest["draft_id"]:
        raise GateFailure("draft id differs from manifest")
    provenance_filename = manifest.get("provenance_file")
    if not provenance_filename:
        raise GateFailure("manifest does not declare timeline bridge provenance")
    provenance_filename = _validated_sidecar_name(provenance_filename)
    provenance_path = draft_path / provenance_filename
    if not provenance_path.is_file():
        raise GateFailure("timeline bridge provenance file is missing")
    if _sha256(provenance_path) != manifest.get("provenance_sha256"):
        raise GateFailure("timeline bridge provenance hash differs from manifest")
    provenance = _read_json(provenance_path)
    videos = {item.get("id") for item in content.get("materials", {}).get("videos", [])}
    video_segments = [
        segment
        for track in content.get("tracks", [])
        for segment in track.get("segments", [])
        if segment.get("material_id") in videos
    ]
    if len(video_segments) != manifest["video_segments"]:
        raise GateFailure("video segment count differs from manifest")
    _validate_generated_provenance(
        provenance,
        manifest=manifest,
        content=content,
        meta=meta,
        jianying_install_dir=jianying_install_dir,
        video_segments=video_segments,
    )
    fps = int(manifest.get("fps", 30))
    previous_end_frame = 0
    source_media_frames = us_to_frame(int(manifest["source_media_duration_us"]), fps)
    for index, segment in enumerate(video_segments):
        target_range = segment["target_timerange"]
        source_range = segment["source_timerange"]
        target_start = int(target_range.get("start", 0))
        target_duration = int(target_range["duration"])
        source_start = int(source_range.get("start", 0))
        source_duration = int(source_range["duration"])
        target_start_frame = checked_us_to_frame(
            target_start, fps, f"segment[{index}].target.start"
        )
        target_end_frame = checked_us_to_frame(
            target_start + target_duration, fps, f"segment[{index}].target.end"
        )
        source_start_frame = checked_us_to_frame(
            source_start, fps, f"segment[{index}].source.start"
        )
        source_end_frame = checked_us_to_frame(
            source_start + source_duration, fps, f"segment[{index}].source.end"
        )
        if (
            target_start_frame != previous_end_frame
            or target_end_frame <= target_start_frame
            or source_end_frame - source_start_frame != target_end_frame - target_start_frame
        ):
            raise GateFailure(f"invalid generated segment range at index {index}")
        if source_end_frame > source_media_frames:
            raise GateFailure(f"generated source range out of bounds at index {index}")
        previous_end_frame = target_end_frame
    if previous_end_frame != checked_us_to_frame(
        int(manifest["duration_us"]), fps, "manifest.duration"
    ):
        raise GateFailure("generated segments do not end at manifest duration")

    text_materials = {
        item.get("id"): item
        for item in content.get("materials", {}).get("texts", [])
        if isinstance(item, dict) and item.get("id")
    }
    caption_segments = [
        segment
        for track in content.get("tracks", [])
        for segment in track.get("segments", [])
        if segment.get("material_id") in text_materials
    ]
    if len(caption_segments) != manifest.get("caption_segments", 0):
        raise GateFailure("caption segment count differs from manifest")
    source_srt = manifest.get("source_srt")
    if source_srt:
        cues = parse_srt(Path(source_srt))
        caption_tolerance_us = (US_PER_SECOND + int(manifest.get("fps", 30)) - 1) // int(
            manifest.get("fps", 30)
        )
        if len(cues) != len(caption_segments):
            raise GateFailure("caption count differs from source SRT")
        for index, (cue, segment) in enumerate(zip(cues, caption_segments)):
            target_range = segment.get("target_timerange", {})
            start_us = int(target_range.get("start", 0))
            duration_us = int(target_range.get("duration", 0))
            if (
                abs(start_us - cue.start_ms * 1000) > caption_tolerance_us
                or abs(
                    start_us
                    + duration_us
                    - cue.end_ms * 1000
                )
                > caption_tolerance_us
            ):
                raise GateFailure(f"caption timing differs from source SRT at index {index}")
            material = text_materials[segment["material_id"]]
            try:
                generated_text = json.loads(material["content"])["text"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise GateFailure(f"invalid generated caption content at index {index}") from exc
            if generated_text != cue.text:
                raise GateFailure(f"caption text differs from source SRT at index {index}")

    registered = None
    journal_archive = None
    if root_meta_info is not None:
        root_payload = _read_json(root_meta_info)
        entries = root_payload.get("all_draft_store", [])
        if not isinstance(entries, list):
            raise GateFailure("root_meta_info all_draft_store is not a list")
        matching_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("draft_id") == manifest["draft_id"]
        ]
        if len(matching_entries) != 1:
            raise GateFailure("root metadata must contain exactly one matching draft id")
        _validate_root_index_entry(
            matching_entries[0],
            manifest=manifest,
            target=draft_path.resolve(),
            draft_root=draft_path.parent.resolve(),
        )
        target_norm = _norm_jy_path(draft_path.resolve()).casefold()
        path_matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and str(entry.get("draft_fold_path", "")).replace("\\", "/").casefold()
            == target_norm
        ]
        if len(path_matches) != 1 or path_matches[0].get("draft_id") != manifest["draft_id"]:
            raise GateFailure("root metadata draft path is missing, duplicated, or owned by another id")
        registered = True
        journal_path = _deployment_journal_path(draft_path.parent.resolve(), manifest["draft_id"])
        if not journal_path.is_file():
            raise GateFailure("deployment journal is missing; formal verification cannot commit")
        journal = _read_json(journal_path)
        if Path(journal.get("target", "")).resolve() != draft_path.resolve():
            raise GateFailure("deployment journal target differs from verified draft")
        if journal.get("state") != "INDEX_COMMITTED":
            raise GateFailure(
                f"deployment journal is not ready for verification: {journal.get('state')!r}"
            )
        _update_deployment_journal(journal_path, journal, "VERIFIED")
        journal_archive = str(
            _archive_deployment_journal(journal_path, report_dir, "verified").resolve()
        )

    result = {
        "status": "passed",
        "draft_path": str(draft_path.resolve()),
        "draft_id": manifest["draft_id"],
        "timeline_id": manifest["timeline_id"],
        "video_segments": len(video_segments),
        "video_materials": len(videos),
        "caption_segments": len(caption_segments),
        "duration_us": frame_to_us(previous_end_frame, fps),
        "duration_frames": previous_end_frame,
        "root_index_registered": registered,
        "deployment_journal_archive": journal_archive,
        "root_content_sha256": _sha256(draft_path / "draft_content.json"),
        "timeline_content_sha256": _sha256(timeline_content_path),
    }
    _write_json_atomic(report_dir / "structural_verification.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FCP7 XML to Jianying 11.2 draft test adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--xml", required=True, type=Path)
    analyze_parser.add_argument("--srt", type=Path)
    analyze_parser.add_argument("--ffprobe", required=True, type=Path)
    analyze_parser.add_argument("--report-dir", required=True, type=Path)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--xml", required=True, type=Path)
    prepare_parser.add_argument("--srt", type=Path)
    prepare_parser.add_argument("--ffprobe", required=True, type=Path)
    prepare_parser.add_argument("--ffmpeg", required=True, type=Path)
    prepare_parser.add_argument("--report-dir", required=True, type=Path)
    prepare_parser.add_argument("--prepared-root", required=True, type=Path)
    prepare_parser.add_argument("--draft-root", required=True, type=Path)
    prepare_parser.add_argument("--draft-name", required=True)
    prepare_parser.add_argument("--reference-draft", required=True, type=Path)
    prepare_parser.add_argument("--jianying-install-dir", required=True, type=Path)

    deploy_parser = subparsers.add_parser("deploy")
    deploy_parser.add_argument("--prepared", required=True, type=Path)
    deploy_parser.add_argument("--manifest", required=True, type=Path)
    deploy_parser.add_argument("--root-meta-info", required=True, type=Path)
    deploy_parser.add_argument("--report-dir", required=True, type=Path)

    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--draft-root", required=True, type=Path)
    recover_parser.add_argument("--root-meta-info", required=True, type=Path)
    recover_parser.add_argument("--report-dir", required=True, type=Path)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--draft", required=True, type=Path)
    verify_parser.add_argument("--manifest", required=True, type=Path)
    verify_parser.add_argument("--jianying-install-dir", required=True, type=Path)
    verify_parser.add_argument("--root-meta-info", type=Path)
    verify_parser.add_argument("--report-dir", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "analyze":
        ir, media, gate_report = analyze(args.xml, args.ffprobe, args.report_dir, args.srt)
        print(json.dumps({"status": gate_report["status"], "clips": len(ir.clips), "media": media["path"]}, ensure_ascii=False))
        return 0
    if args.command == "prepare":
        ir, media, _ = analyze(args.xml, args.ffprobe, args.report_dir, args.srt)
        output = prepare_draft(
            ir,
            media,
            args.report_dir,
            args.prepared_root,
            args.draft_root,
            args.draft_name,
            args.reference_draft,
            args.jianying_install_dir,
            args.ffmpeg,
            args.srt,
        )
        verify_draft(
            output,
            args.report_dir / "prepared_manifest.json",
            args.jianying_install_dir,
            None,
            args.report_dir,
        )
        print(json.dumps({"status": "prepared", "path": str(output)}, ensure_ascii=False))
        return 0
    if args.command == "deploy":
        target = deploy(args.prepared, args.manifest, args.root_meta_info, args.report_dir)
        print(json.dumps({"status": "deployed", "path": str(target)}, ensure_ascii=False))
        return 0
    if args.command == "recover":
        result = recover_deployments(args.draft_root, args.root_meta_info, args.report_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "verify":
        result = verify_draft(
            args.draft,
            args.manifest,
            args.jianying_install_dir,
            args.root_meta_info,
            args.report_dir,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateFailure, FileExistsError, FileNotFoundError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
