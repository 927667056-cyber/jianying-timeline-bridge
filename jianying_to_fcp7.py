from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
FORK_DIR = SCRIPT_DIR / "pyJianYingDraft-fork"
if str(FORK_DIR) not in sys.path:
    sys.path.insert(0, str(FORK_DIR))

from pyJianYingDraft import DraftCryptoConfig, JianyingDraftCryptoCodec  # noqa: E402

from jianying_xml_adapter import (  # noqa: E402
    GateFailure,
    US_PER_SECOND,
    frame_to_us,
    parse_fcp7_xml,
    parse_srt,
    probe_media,
)
from bridge_safety import (  # noqa: E402
    PROVENANCE_FILENAME,
    detect_supported_build,
    require_jianying_closed,
    sampled_media_fingerprint,
    sha256_file,
    stable_read_bytes,
)


FRAME_BOUNDARY_TOLERANCE_US = 2


@dataclass(frozen=True)
class ReverseClip:
    index: int
    segment_id: str
    target_start: int
    target_end: int
    source_in: int
    source_out: int

    @property
    def duration(self) -> int:
        return self.target_end - self.target_start


@dataclass(frozen=True)
class CaptionCue:
    index: int
    segment_id: str
    start_us: int
    end_us: int
    text: str


@dataclass
class ReverseIR:
    draft_path: str
    draft_name: str
    timeline_id: str
    sequence_name: str
    fps: int
    fps_source: str
    width: int
    height: int
    duration_frames: int
    media_path: str
    media_duration_frames: int
    media_duration_us: int
    audio_channels: int
    audio_sample_rate: int
    video_track_id: str
    caption_track_id: Optional[str]
    clips: List[ReverseClip]
    captions: List[CaptionCue]
    identity_aux_counts: Dict[str, int]
    material_duration_frames: List[int]

    def serializable(self) -> Dict[str, Any]:
        result = asdict(self)
        result["clips"] = [asdict(clip) for clip in self.clips]
        result["captions"] = [asdict(cue) for cue in self.captions]
        return result


def _require_dict(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise GateFailure(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise GateFailure(f"{label} must be an array")
    return value


def _require_int(value: Any, label: str, *, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GateFailure(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise GateFailure(f"{label} must be >= {minimum}, got {value}")
    return value


def _require_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateFailure(f"{label} must be a non-empty string")
    return value


def _reject_unknown_keys(obj: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise GateFailure(f"{label} has unsupported fields: {unknown}")


def _range_us(value: Any, label: str) -> Tuple[int, int]:
    timerange = _require_dict(value, label)
    _reject_unknown_keys(timerange, {"start", "duration"}, label)
    start = _require_int(timerange.get("start", 0), f"{label}.start", minimum=0)
    duration = _require_int(timerange.get("duration"), f"{label}.duration", minimum=1)
    return start, start + duration


def us_to_frame(value_us: int, fps: int) -> int:
    if value_us < 0:
        raise ValueError("microseconds must be non-negative")
    return (value_us * fps + US_PER_SECOND // 2) // US_PER_SECOND


def _frame_boundary(value_us: int, fps: int, label: str) -> int:
    frame = us_to_frame(value_us, fps)
    canonical = frame_to_us(frame, fps)
    if abs(canonical - value_us) > FRAME_BOUNDARY_TOLERANCE_US:
        raise GateFailure(
            f"{label}={value_us}us is not on a {fps}fps frame boundary "
            f"(nearest frame {frame} is {canonical}us)"
        )
    return frame


def _empty(value: Any) -> bool:
    return value in (None, {}, [], "")


def _same_number(value: Any, expected: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(float(value) - expected) < 1e-9


def _validate_identity_clip(segment: Mapping[str, Any], label: str) -> None:
    if segment.get("source") != "segmentsourcenormal":
        raise GateFailure(f"{label} has unsupported source mode: {segment.get('source')!r}")
    for key in ("enable_hsl", "enable_adjust_mask", "enable_lut", "enable_adjust"):
        if key in segment and segment[key] is not False:
            raise GateFailure(f"{label}.{key} must be false")
    for key in ("render_timerange", "responsive_layout", "uniform_scale"):
        if key in segment and not _empty(segment[key]):
            raise GateFailure(f"{label}.{key} contains an unsupported effect or animation")
    hdr = segment.get("hdr_settings")
    if hdr not in (None, {}, {"mode": 1}):
        raise GateFailure(f"{label}.hdr_settings is not the identity/default value")
    clip = _require_dict(segment.get("clip"), f"{label}.clip")
    _reject_unknown_keys(clip, {"scale", "transform", "flip"}, f"{label}.clip")
    scale = _require_dict(clip.get("scale"), f"{label}.clip.scale")
    transform = _require_dict(clip.get("transform"), f"{label}.clip.transform")
    if not (_same_number(scale.get("x"), 1.0) and _same_number(scale.get("y"), 1.0)):
        raise GateFailure(f"{label} has non-identity scale")
    if not (_same_number(transform.get("x"), 0.0) and _same_number(transform.get("y"), 0.0)):
        raise GateFailure(f"{label} has non-identity transform")
    if not _empty(clip.get("flip")):
        raise GateFailure(f"{label} has a flip effect")


def _detect_fps(content: Mapping[str, Any], requested_fps: Optional[int]) -> Tuple[int, str]:
    candidates: List[Tuple[str, Any]] = [("draft_content.fps", content.get("fps"))]
    config = content.get("config")
    if isinstance(config, dict):
        candidates.append(("draft_content.config.fps", config.get("fps")))
    assistant = content.get("function_assistant_info")
    if isinstance(assistant, dict):
        candidates.append(("draft_content.function_assistant_info.fps", assistant.get("fps")))

    detected: List[Tuple[str, int]] = []
    for label, value in candidates:
        if value in (None, {}, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise GateFailure(f"{label} is not a supported integer frame rate: {value!r}")
        detected.append((label, int(value)))
    if detected and len({value for _, value in detected}) != 1:
        raise GateFailure(f"draft contains conflicting frame-rate fields: {detected}")

    if requested_fps is not None:
        if requested_fps <= 0:
            raise GateFailure("--fps must be positive")
        if detected and detected[0][1] != requested_fps:
            raise GateFailure(
                f"requested fps {requested_fps} differs from {detected[0][0]}={detected[0][1]}"
            )
        return requested_fps, "command_line" if not detected else detected[0][0]
    if detected:
        return detected[0][1], detected[0][0]
    return 30, "fallback_30fps"


def _discover_install_dir(explicit: Optional[Path], draft_path: Path) -> Path:
    candidates: List[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    env_path = os.environ.get("JY_INSTALL_DIR")
    if env_path:
        candidates.append(Path(env_path))
    sibling_root = draft_path.parent.parent / "JianyingPro"
    if sibling_root.is_dir():
        versioned = sorted(
            (item.parent for item in sibling_root.glob("*/JianyingPro.exe")),
            key=lambda item: tuple(int(part) if part.isdigit() else 0 for part in item.name.split(".")),
            reverse=True,
        )
        candidates.extend(versioned)
    candidates.extend(
        [
            Path(r"C:\Program Files\JianyingPro"),
        ]
    )
    for candidate in candidates:
        candidate = candidate.resolve()
        if (candidate / "JianyingPro.exe").is_file():
            return candidate
    raise GateFailure("cannot locate JianyingPro install directory; pass --jianying-install-dir")


def _discover_ffprobe(explicit: Optional[Path]) -> Path:
    candidates: List[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    found = shutil.which("ffprobe")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_file():
            return candidate
    raise GateFailure("cannot locate ffprobe; pass --ffprobe")


def _load_draft(
    draft_path: Path,
    install_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str]:
    content_path = draft_path / "draft_content.json"
    meta_path = draft_path / "draft_meta_info.json"
    project_path = draft_path / "Timelines" / "project.json"
    for path in (content_path, meta_path, project_path):
        if not path.is_file():
            raise GateFailure(f"required draft file is missing: {path}")

    codec = JianyingDraftCryptoCodec(
        DraftCryptoConfig(
            jy_install_dir=str(install_dir),
            isolated=True,
            validate_roundtrip=False,
            backup=False,
        )
    )
    try:
        content_raw, _ = stable_read_bytes(content_path)
        meta_raw, _ = stable_read_bytes(meta_path)
        content = codec.decode(content_raw)
        meta = codec.decode(meta_raw)
    except Exception as exc:
        raise GateFailure(f"failed to decrypt Jianying 11.2 draft JSON: {exc}") from exc
    project_raw, _ = stable_read_bytes(project_path)
    project = json.loads(project_raw.decode("utf-8-sig"))
    project = _require_dict(project, "Timelines/project.json")
    main_timeline_id = _require_id(project.get("main_timeline_id"), "project.main_timeline_id")
    timelines = _require_list(project.get("timelines"), "project.timelines")
    active = [item for item in timelines if isinstance(item, dict) and not item.get("is_marked_delete", False)]
    if len(active) != 1 or active[0].get("id") != main_timeline_id:
        raise GateFailure("draft must contain exactly one active main timeline")
    if content.get("id") != main_timeline_id:
        raise GateFailure("root draft_content id differs from project main_timeline_id")

    timeline_content_path = draft_path / "Timelines" / main_timeline_id / "draft_content.json"
    if not timeline_content_path.is_file():
        raise GateFailure(f"main timeline content is missing: {timeline_content_path}")
    try:
        timeline_raw, _ = stable_read_bytes(timeline_content_path)
        timeline_content = codec.decode(timeline_raw)
    except Exception as exc:
        raise GateFailure(f"failed to decrypt main timeline draft_content: {exc}") from exc
    if content != timeline_content:
        raise GateFailure("root and main-timeline draft_content differ")
    return content, meta, project, main_timeline_id


SAFE_MATERIAL_ARRAYS = {
    "videos",
    "texts",
    "canvases",
    "audio_fades",
    "material_animations",
    "placeholder_infos",
    "speeds",
    "sound_channel_mappings",
    "material_colors",
    "vocal_separations",
}


def _index_materials(
    materials: Mapping[str, Any],
) -> Tuple[Dict[str, Tuple[str, Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    by_id: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    arrays: Dict[str, List[Dict[str, Any]]] = {}
    for array_name, raw_items in materials.items():
        items = _require_list(raw_items, f"materials.{array_name}")
        if array_name not in SAFE_MATERIAL_ARRAYS and items:
            raise GateFailure(f"unsupported non-empty material array: materials.{array_name}")
        typed_items: List[Dict[str, Any]] = []
        for index, raw_item in enumerate(items):
            item = _require_dict(raw_item, f"materials.{array_name}[{index}]")
            typed_items.append(item)
            material_id = item.get("id")
            if material_id is None:
                if items:
                    raise GateFailure(f"materials.{array_name}[{index}] has no id")
                continue
            material_id = _require_id(material_id, f"materials.{array_name}[{index}].id")
            if material_id in by_id:
                raise GateFailure(f"duplicate material id across arrays: {material_id}")
            by_id[material_id] = (array_name, item)
        arrays[array_name] = typed_items
    for required in SAFE_MATERIAL_ARRAYS:
        arrays.setdefault(required, [])
    return by_id, arrays


def _classify_tracks(
    tracks: List[Any], by_id: Mapping[str, Tuple[str, Dict[str, Any]]]
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    video_tracks: List[Dict[str, Any]] = []
    caption_tracks: List[Dict[str, Any]] = []
    for index, raw_track in enumerate(tracks):
        track = _require_dict(raw_track, f"tracks[{index}]")
        _reject_unknown_keys(track, {"id", "type", "segments", "flag", "is_default_name"}, f"tracks[{index}]")
        _require_id(track.get("id"), f"tracks[{index}].id")
        if track.get("type") != "mixed":
            raise GateFailure(f"tracks[{index}] has unsupported type: {track.get('type')!r}")
        segments = _require_list(track.get("segments"), f"tracks[{index}].segments")
        if not segments:
            raise GateFailure(f"tracks[{index}] is empty; unknown/empty tracks are rejected")
        kinds: set[str] = set()
        for segment_index, raw_segment in enumerate(segments):
            segment = _require_dict(raw_segment, f"tracks[{index}].segments[{segment_index}]")
            material_id = _require_id(
                segment.get("material_id"), f"tracks[{index}].segments[{segment_index}].material_id"
            )
            material_entry = by_id.get(material_id)
            if material_entry is None:
                raise GateFailure(f"segment references an unknown material id: {material_id}")
            kinds.add(material_entry[0])
        if kinds == {"videos"}:
            if track.get("flag") not in (None, 0):
                raise GateFailure(f"video track {index} has unsupported flag: {track.get('flag')!r}")
            video_tracks.append(track)
        elif kinds == {"texts"}:
            if track.get("flag") != 2:
                raise GateFailure(f"caption track {index} must have flag=2")
            caption_tracks.append(track)
        else:
            raise GateFailure(f"tracks[{index}] mixes or uses unsupported material arrays: {sorted(kinds)}")
    if len(video_tracks) != 1:
        raise GateFailure(f"expected exactly one video track, got {len(video_tracks)}")
    if len(caption_tracks) > 1:
        raise GateFailure(f"expected at most one caption track, got {len(caption_tracks)}")
    if len(video_tracks) + len(caption_tracks) != len(tracks):
        raise GateFailure("draft contains an unknown track")
    return video_tracks[0], caption_tracks[0] if caption_tracks else None


VIDEO_MATERIAL_KEYS = {
    "id",
    "type",
    "duration",
    "path",
    "width",
    "height",
    "category_name",
    "material_id",
    "material_name",
    "crop",
    "stable",
    "matting",
    "check_flag",
    "video_algorithm",
    "local_material_id",
    "beauty_face_auto_preset",
    "video_mask_stroke",
    "video_mask_shadow",
    "audio_fade",
}


def _validate_video_material(material: Mapping[str, Any], label: str) -> None:
    _reject_unknown_keys(material, VIDEO_MATERIAL_KEYS, label)
    if material.get("type") != "video":
        raise GateFailure(f"{label}.type must be video")
    if not isinstance(material.get("path"), str) or not material["path"].strip():
        raise GateFailure(f"{label}.path is missing")
    _require_int(material.get("duration"), f"{label}.duration", minimum=1)
    _require_int(material.get("width"), f"{label}.width", minimum=1)
    _require_int(material.get("height"), f"{label}.height", minimum=1)
    if not _empty(material.get("crop")):
        raise GateFailure(f"{label} has a crop effect")
    if material.get("stable") not in (None, {}, {"time_range": {}}):
        raise GateFailure(f"{label} has stabilization settings")
    matting = material.get("matting")
    if isinstance(matting, dict) and not _empty(matting.get("path")):
        raise GateFailure(f"{label} has matting")
    algorithm = material.get("video_algorithm")
    if isinstance(algorithm, dict):
        if not _empty(algorithm.get("path")) or not _empty(algorithm.get("story_video_modify_video_config")):
            raise GateFailure(f"{label} has video algorithm effects")
    if not _empty(material.get("beauty_face_auto_preset")):
        raise GateFailure(f"{label} has a beauty preset")
    mask_stroke = material.get("video_mask_stroke")
    if isinstance(mask_stroke, dict) and any(not _empty(mask_stroke.get(key)) for key in ("resource_id", "path", "type")):
        raise GateFailure(f"{label} has a video mask stroke")
    mask_shadow = material.get("video_mask_shadow")
    if isinstance(mask_shadow, dict) and any(not _empty(mask_shadow.get(key)) for key in ("resource_id", "path")):
        raise GateFailure(f"{label} has a video mask shadow")
    audio_fade = material.get("audio_fade")
    if audio_fade is not None:
        audio_fade = _require_dict(audio_fade, f"{label}.audio_fade")
        _reject_unknown_keys(audio_fade, {"id", "type"}, f"{label}.audio_fade")
        _require_id(audio_fade.get("id"), f"{label}.audio_fade.id")
        if audio_fade.get("type") != "audio_fade":
            raise GateFailure(f"{label}.audio_fade has unsupported parameters")


IDENTITY_AUX_SCHEMAS: Dict[str, Tuple[set[str], Dict[str, Any]]] = {
    "speeds": ({"id", "type"}, {"type": "speed"}),
    "placeholder_infos": ({"id", "type", "meta_type"}, {"type": "placeholder_info", "meta_type": "none"}),
    "canvases": ({"id", "type"}, {"type": "canvas_color"}),
    "sound_channel_mappings": ({"id", "type"}, {"type": "none"}),
    "material_colors": ({"id"}, {}),
    "vocal_separations": ({"id", "type"}, {"type": "vocal_separation"}),
    # Jianying 11.2 may add a bare marker at a cut; parameters would be a real fade and are rejected.
    "audio_fades": ({"id", "type"}, {"type": "audio_fade"}),
}


def _validate_identity_aux(array_name: str, item: Mapping[str, Any], label: str) -> None:
    allowed, expected = IDENTITY_AUX_SCHEMAS[array_name]
    _reject_unknown_keys(item, allowed, label)
    for key, value in expected.items():
        if item.get(key) != value:
            raise GateFailure(f"{label}.{key} must be {value!r}")


def _material_path(raw_path: str) -> Path:
    return Path(raw_path.replace("/", os.sep)).resolve()


def _validate_video_track(
    track: Mapping[str, Any],
    by_id: Mapping[str, Tuple[str, Dict[str, Any]]],
    arrays: Mapping[str, List[Dict[str, Any]]],
    fps: int,
) -> Tuple[List[ReverseClip], Path, int, int, Counter[str]]:
    allowed_segment_keys = {
        "id",
        "source_timerange",
        "target_timerange",
        "render_timerange",
        "clip",
        "uniform_scale",
        "material_id",
        "extra_material_refs",
        "enable_hsl",
        "enable_lut",
        "enable_adjust",
        "hdr_settings",
        "responsive_layout",
        "enable_adjust_mask",
        "source",
    }
    clips: List[ReverseClip] = []
    referenced_video_ids: set[str] = set()
    referenced_aux_ids: set[str] = set()
    media_paths: set[Path] = set()
    dimensions: set[Tuple[int, int]] = set()
    material_duration_us: set[int] = set()
    aux_counts: Counter[str] = Counter()
    previous_target_end = 0

    for index, raw_segment in enumerate(_require_list(track.get("segments"), "video_track.segments")):
        segment = _require_dict(raw_segment, f"video_track.segments[{index}]")
        _reject_unknown_keys(segment, allowed_segment_keys, f"video_track.segments[{index}]")
        _validate_identity_clip(segment, f"video_track.segments[{index}]")
        segment_id = _require_id(segment.get("id"), f"video_track.segments[{index}].id")
        material_id = _require_id(segment.get("material_id"), f"video_track.segments[{index}].material_id")
        array_name, material = by_id[material_id]
        if array_name != "videos":
            raise GateFailure(f"video segment {index} does not reference a video material")
        _validate_video_material(material, f"materials.videos[{material_id}]")
        referenced_video_ids.add(material_id)
        media_paths.add(_material_path(str(material["path"])))
        dimensions.add((int(material["width"]), int(material["height"])))
        material_duration_us.add(int(material["duration"]))

        refs = _require_list(segment.get("extra_material_refs"), f"video_track.segments[{index}].extra_material_refs")
        if len(refs) != len(set(refs)):
            raise GateFailure(f"video segment {index} has duplicate extra_material_refs")
        per_segment: Counter[str] = Counter()
        for ref in refs:
            ref_id = _require_id(ref, f"video_track.segments[{index}].extra_material_refs")
            entry = by_id.get(ref_id)
            if entry is None or entry[0] not in IDENTITY_AUX_SCHEMAS:
                raise GateFailure(f"video segment {index} has unknown/non-identity extra material ref: {ref_id}")
            _validate_identity_aux(entry[0], entry[1], f"materials.{entry[0]}[{ref_id}]")
            per_segment[entry[0]] += 1
            referenced_aux_ids.add(ref_id)
            aux_counts[entry[0]] += 1
        for required in (
            "speeds",
            "placeholder_infos",
            "canvases",
            "sound_channel_mappings",
            "material_colors",
            "vocal_separations",
        ):
            if per_segment[required] != 1:
                raise GateFailure(f"video segment {index} must reference exactly one identity {required} material")
        if per_segment["audio_fades"] > 1:
            raise GateFailure(f"video segment {index} references multiple audio fade markers")
        nested_audio_fade = material.get("audio_fade")
        if nested_audio_fade is not None:
            nested_fade_id = str(nested_audio_fade["id"])
            if nested_fade_id not in refs or by_id.get(nested_fade_id, (None,))[0] != "audio_fades":
                raise GateFailure(f"video segment {index} has an inconsistent bare audio_fade marker")
        elif per_segment["audio_fades"]:
            raise GateFailure(f"video segment {index} references audio_fades without the matching bare marker")

        target_start_us, target_end_us = _range_us(
            segment.get("target_timerange"), f"video_track.segments[{index}].target_timerange"
        )
        source_start_us, source_end_us = _range_us(
            segment.get("source_timerange"), f"video_track.segments[{index}].source_timerange"
        )
        target_start = _frame_boundary(target_start_us, fps, f"video[{index}].target_start")
        target_end = _frame_boundary(target_end_us, fps, f"video[{index}].target_end")
        source_in = _frame_boundary(source_start_us, fps, f"video[{index}].source_in")
        source_out = _frame_boundary(source_end_us, fps, f"video[{index}].source_out")
        if target_start != previous_target_end:
            relation = "gap" if target_start > previous_target_end else "overlap"
            raise GateFailure(
                f"video timeline {relation} at segment {index}: expected {previous_target_end}, got {target_start}"
            )
        if target_end <= target_start or source_out <= source_in:
            raise GateFailure(f"video segment {index} has a non-positive range")
        if target_end - target_start != source_out - source_in:
            raise GateFailure(f"video segment {index} is retimed/variable-speed")
        clips.append(
            ReverseClip(
                index=index,
                segment_id=segment_id,
                target_start=target_start,
                target_end=target_end,
                source_in=source_in,
                source_out=source_out,
            )
        )
        previous_target_end = target_end

    if len(media_paths) != 1:
        raise GateFailure(f"video track references multiple media paths: {sorted(map(str, media_paths))}")
    if len(dimensions) != 1:
        raise GateFailure(f"video materials disagree on dimensions: {sorted(dimensions)}")
    if not material_duration_us:
        raise GateFailure("video track has no source duration")
    all_video_ids = {_require_id(item.get("id"), "materials.videos.id") for item in arrays["videos"]}
    if referenced_video_ids != all_video_ids:
        raise GateFailure("materials.videos contains orphan or unreferenced materials")
    all_aux_ids = {
        _require_id(item.get("id"), f"materials.{name}.id")
        for name in IDENTITY_AUX_SCHEMAS
        for item in arrays[name]
    }
    if referenced_aux_ids != all_aux_ids:
        raise GateFailure("identity auxiliary arrays contain orphan or unreferenced materials")
    width, height = next(iter(dimensions))
    return clips, next(iter(media_paths)), width, height, aux_counts


TEXT_MATERIAL_KEYS = {
    "id",
    "type",
    "content",
    "words",
    "current_words",
    "combo_info",
    "caption_template_info",
    "layer_weight",
    "line_spacing",
    "shadow_alpha",
    "shadow_smoothing",
    "shadow_distance",
    "shadow_point",
    "shadow_angle",
    "border_width",
    "text_color",
    "font_size",
    "font_path",
    "initial_scale",
    "add_type",
    "group_id",
    "lyrics_template",
}


def _resource_info_empty(value: Any, label: str) -> None:
    if value is None:
        return
    info = _require_dict(value, label)
    if any(not _empty(info.get(key)) for key in ("resource_id", "path")):
        raise GateFailure(f"{label} references an unsupported caption template/effect")


def _caption_text(material: Mapping[str, Any], label: str) -> str:
    _reject_unknown_keys(material, TEXT_MATERIAL_KEYS, label)
    if material.get("type") != "subtitle":
        raise GateFailure(f"{label}.type must be subtitle")
    for key in ("words", "current_words", "combo_info"):
        if not _empty(material.get(key)):
            raise GateFailure(f"{label}.{key} is non-empty; karaoke/word effects are unsupported")
    _resource_info_empty(material.get("caption_template_info"), f"{label}.caption_template_info")
    _resource_info_empty(material.get("lyrics_template"), f"{label}.lyrics_template")
    raw_content = material.get("content")
    if not isinstance(raw_content, str):
        raise GateFailure(f"{label}.content must be a JSON string")
    try:
        content = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise GateFailure(f"{label}.content is invalid nested JSON") from exc
    content = _require_dict(content, f"{label}.content")
    _reject_unknown_keys(content, {"styles", "text"}, f"{label}.content")
    text = content.get("text")
    if not isinstance(text, str) or not text.strip():
        raise GateFailure(f"{label}.content.text is empty")
    if re.search(r"[\r\n]", text) or r"\N" in text or r"\\n" in text:
        raise GateFailure(f"{label}.content.text is not a single line")
    return text


def _validate_caption_track(
    track: Optional[Mapping[str, Any]],
    by_id: Mapping[str, Tuple[str, Dict[str, Any]]],
    arrays: Mapping[str, List[Dict[str, Any]]],
    timeline_duration_us: int,
) -> List[CaptionCue]:
    if track is None:
        if arrays["texts"] or arrays["material_animations"]:
            raise GateFailure("caption materials exist without a caption track")
        return []
    allowed_segment_keys = {
        "id",
        "target_timerange",
        "render_timerange",
        "clip",
        "uniform_scale",
        "material_id",
        "extra_material_refs",
        "render_index",
        "enable_lut",
        "enable_adjust",
        "enable_hsl",
        "track_render_index",
        "responsive_layout",
        "enable_adjust_mask",
        "source",
    }
    cues: List[CaptionCue] = []
    referenced_texts: set[str] = set()
    referenced_animations: set[str] = set()
    previous_end = 0
    for index, raw_segment in enumerate(_require_list(track.get("segments"), "caption_track.segments")):
        segment = _require_dict(raw_segment, f"caption_track.segments[{index}]")
        _reject_unknown_keys(segment, allowed_segment_keys, f"caption_track.segments[{index}]")
        if "source_timerange" in segment:
            raise GateFailure(f"caption segment {index} unexpectedly has source_timerange")
        if segment.get("source") != "segmentsourcenormal":
            raise GateFailure(f"caption segment {index} has unsupported source mode")
        for key in ("enable_lut", "enable_adjust", "enable_hsl", "enable_adjust_mask"):
            if segment.get(key) is not False:
                raise GateFailure(f"caption segment {index}.{key} must be false")
        for key in ("render_timerange", "responsive_layout", "uniform_scale"):
            if not _empty(segment.get(key)):
                raise GateFailure(f"caption segment {index}.{key} is non-empty")
        if segment.get("track_render_index") != 1:
            raise GateFailure(f"caption segment {index}.track_render_index must be 1")
        _require_int(segment.get("render_index"), f"caption segment {index}.render_index")

        segment_id = _require_id(segment.get("id"), f"caption segment {index}.id")
        material_id = _require_id(segment.get("material_id"), f"caption segment {index}.material_id")
        array_name, material = by_id[material_id]
        if array_name != "texts":
            raise GateFailure(f"caption segment {index} does not reference materials.texts")
        if material_id in referenced_texts:
            raise GateFailure(f"caption text material is referenced more than once: {material_id}")
        referenced_texts.add(material_id)
        text = _caption_text(material, f"materials.texts[{material_id}]")

        refs = _require_list(segment.get("extra_material_refs"), f"caption segment {index}.extra_material_refs")
        if len(refs) != 1:
            raise GateFailure(f"caption segment {index} must reference exactly one bare material animation")
        animation_id = _require_id(refs[0], f"caption segment {index}.extra_material_refs[0]")
        animation_entry = by_id.get(animation_id)
        if animation_entry is None or animation_entry[0] != "material_animations":
            raise GateFailure(f"caption segment {index} animation reference is missing or unknown")
        animation = animation_entry[1]
        _reject_unknown_keys(animation, {"id", "type"}, f"materials.material_animations[{animation_id}]")
        if animation.get("type") != "sticker_animation":
            raise GateFailure(f"caption segment {index} has a non-default animation")
        if animation_id in referenced_animations:
            raise GateFailure(f"caption animation is referenced more than once: {animation_id}")
        referenced_animations.add(animation_id)

        start_us, end_us = _range_us(segment.get("target_timerange"), f"caption segment {index}.target_timerange")
        if start_us < previous_end:
            raise GateFailure(f"caption segments overlap at index {index}")
        if end_us > timeline_duration_us + FRAME_BOUNDARY_TOLERANCE_US:
            raise GateFailure(f"caption segment {index} exceeds the video timeline")
        cues.append(CaptionCue(index=index + 1, segment_id=segment_id, start_us=start_us, end_us=end_us, text=text))
        previous_end = end_us

    all_text_ids = {_require_id(item.get("id"), "materials.texts.id") for item in arrays["texts"]}
    all_animation_ids = {
        _require_id(item.get("id"), "materials.material_animations.id")
        for item in arrays["material_animations"]
    }
    if referenced_texts != all_text_ids:
        raise GateFailure("materials.texts contains orphan or unreferenced materials")
    if referenced_animations != all_animation_ids:
        raise GateFailure("materials.material_animations contains orphan or unreferenced materials")
    return cues


def _load_provenance(
    draft_path: Path,
    *,
    timeline_id: str,
    meta: Mapping[str, Any],
    build_version: str,
    dll_sha256: str,
    required: bool,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    provenance_path = draft_path / PROVENANCE_FILENAME
    if not provenance_path.is_file():
        if required:
            raise GateFailure(
                f"managed provenance is missing: {provenance_path}. "
                "Unmanaged Jianying drafts may be diagnosed, but are not eligible for a formal lossless export."
            )
        return None, {"managed": False, "path": None, "sha256": None}

    raw, digest = stable_read_bytes(provenance_path)
    try:
        provenance = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateFailure("timeline bridge provenance is not valid JSON") from exc
    provenance = _require_dict(provenance, "timeline bridge provenance")
    if provenance.get("schema") != "io.github.jianying-timeline-bridge.provenance":
        raise GateFailure("timeline bridge provenance has an unsupported schema")
    if provenance.get("schema_version") != 1:
        raise GateFailure("timeline bridge provenance has an unsupported schema version")

    draft_info = _require_dict(provenance.get("draft"), "provenance.draft")
    if draft_info.get("timeline_id") != timeline_id:
        raise GateFailure("provenance timeline id differs from the active draft timeline")
    if draft_info.get("draft_id") != meta.get("draft_id"):
        raise GateFailure("provenance draft id differs from draft_meta_info")
    if draft_info.get("draft_name") != meta.get("draft_name"):
        raise GateFailure("provenance draft name differs from draft_meta_info")

    compatibility = _require_dict(provenance.get("compatibility"), "provenance.compatibility")
    if compatibility.get("jianying_build") != build_version:
        raise GateFailure("provenance Jianying build differs from the calibrated local build")
    if str(compatibility.get("videoeditor_dll_sha256", "")).lower() != dll_sha256.lower():
        raise GateFailure("provenance DLL fingerprint differs from the calibrated local build")

    timeline = _require_dict(provenance.get("timeline"), "provenance.timeline")
    segment_map = _require_list(provenance.get("segment_map"), "provenance.segment_map")
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(segment_map):
        entry = _require_dict(raw_entry, f"provenance.segment_map[{index}]")
        segment_id = _require_id(entry.get("segment_id"), f"provenance.segment_map[{index}].segment_id")
        if segment_id in seen_ids:
            raise GateFailure("provenance segment map contains a duplicate segment id")
        seen_ids.add(segment_id)
        for key in (
            "clip_index",
            "target_start_frame",
            "target_end_frame",
            "source_in_frame",
            "source_out_frame",
        ):
            _require_int(entry.get(key), f"provenance.segment_map[{index}].{key}", minimum=0)

    source = _require_dict(provenance.get("source"), "provenance.source")
    xml_info = _require_dict(source.get("xml"), "provenance.source.xml")
    source_xml_path = Path(str(xml_info.get("path", "")))
    if source_xml_path.is_file() and sha256_file(source_xml_path) != xml_info.get("sha256"):
        raise GateFailure("the source XML has changed since this managed draft was created")
    _require_dict(source.get("media"), "provenance.source.media")
    return provenance, {
        "managed": True,
        "path": str(provenance_path),
        "sha256": digest,
        "bridge_version": provenance.get("bridge_version"),
        "original_segments": len(segment_map),
        "original_sequence_name": timeline.get("sequence_name"),
    }


def _build_edit_diff(
    provenance: Optional[Mapping[str, Any]], clips: Sequence[ReverseClip]
) -> Dict[str, Any]:
    if provenance is None:
        return {"status": "unmanaged", "unchanged": 0, "modified": 0, "deleted": 0, "added": 0}
    original_entries = _require_list(provenance.get("segment_map"), "provenance.segment_map")
    original_by_id = {
        str(entry["segment_id"]): entry for entry in original_entries if isinstance(entry, dict)
    }
    current_by_id = {clip.segment_id: clip for clip in clips}
    unchanged: List[str] = []
    modified: List[Dict[str, Any]] = []
    for segment_id, entry in original_by_id.items():
        clip = current_by_id.get(segment_id)
        if clip is None:
            continue
        before = {
            "target_start": int(entry["target_start_frame"]),
            "target_end": int(entry["target_end_frame"]),
            "source_in": int(entry["source_in_frame"]),
            "source_out": int(entry["source_out_frame"]),
        }
        after = {
            "target_start": clip.target_start,
            "target_end": clip.target_end,
            "source_in": clip.source_in,
            "source_out": clip.source_out,
        }
        if before == after:
            unchanged.append(segment_id)
        else:
            modified.append({"segment_id": segment_id, "before": before, "after": after})
    deleted = [segment_id for segment_id in original_by_id if segment_id not in current_by_id]
    added = [clip.segment_id for clip in clips if clip.segment_id not in original_by_id]
    original_order = {str(entry["segment_id"]): index for index, entry in enumerate(original_entries)}
    shared_order = [original_order[clip.segment_id] for clip in clips if clip.segment_id in original_order]
    reordered = any(left > right for left, right in zip(shared_order, shared_order[1:]))
    status = "strict_no_edit" if not modified and not deleted and not added and not reordered else "supported_edit"
    return {
        "status": status,
        "unchanged": len(unchanged),
        "modified": len(modified),
        "deleted": len(deleted),
        "added": len(added),
        "reordered": reordered,
        "modified_segments": modified,
        "deleted_segment_ids": deleted,
        "added_segment_ids": added,
    }


def _timeline_semantic_sha256(ir: ReverseIR) -> str:
    canonical = {
        "fps": ir.fps,
        "width": ir.width,
        "height": ir.height,
        "duration_frames": ir.duration_frames,
        "media_path": str(Path(ir.media_path).resolve()).casefold(),
        "media_duration_frames": ir.media_duration_frames,
        "clips": [
            [clip.target_start, clip.target_end, clip.source_in, clip.source_out]
            for clip in ir.clips
        ],
        "captions": [[cue.start_us, cue.end_us, cue.text] for cue in ir.captions],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def analyze_draft(
    draft_path: Path,
    *,
    jianying_install_dir: Optional[Path] = None,
    ffprobe_path: Optional[Path] = None,
    fps: Optional[int] = None,
    sequence_name: Optional[str] = None,
    require_provenance: bool = True,
) -> Tuple[ReverseIR, Dict[str, Any]]:
    draft_path = draft_path.resolve()
    if not draft_path.is_dir():
        raise GateFailure(f"draft directory does not exist: {draft_path}")
    require_jianying_closed()
    install_dir = _discover_install_dir(jianying_install_dir, draft_path)
    calibrated_build = detect_supported_build(install_dir)
    ffprobe = _discover_ffprobe(ffprobe_path)
    content, meta, project, timeline_id = _load_draft(draft_path, install_dir)
    provenance, provenance_status = _load_provenance(
        draft_path,
        timeline_id=timeline_id,
        meta=meta,
        build_version=calibrated_build.version,
        dll_sha256=calibrated_build.dll_sha256,
        required=require_provenance,
    )
    selected_fps, fps_source = _detect_fps(content, fps)
    if selected_fps != 30:
        raise GateFailure(f"v1 supports only the calibrated 30/1 fps profile, got {selected_fps}")
    if (content.get("new_version") or content.get("version")) != "181.0.0":
        raise GateFailure("draft content version is outside the calibrated Jianying 11.2 profile")
    if _require_dict(content.get("platform", {}), "platform").get("app_version") != "11.2.0":
        raise GateFailure("draft app version is outside the calibrated Jianying 11.2 profile")
    if not _empty(content.get("keyframes")):
        raise GateFailure("draft contains keyframes/animation")

    materials = _require_dict(content.get("materials"), "draft_content.materials")
    by_id, arrays = _index_materials(materials)
    tracks = _require_list(content.get("tracks"), "draft_content.tracks")
    video_track, caption_track = _classify_tracks(tracks, by_id)
    clips, media_path, material_width, material_height, aux_counts = _validate_video_track(
        video_track, by_id, arrays, selected_fps
    )
    if not clips:
        raise GateFailure("video track contains no clips")

    timeline_duration_us = _require_int(content.get("duration"), "draft_content.duration", minimum=1)
    meta_duration_us = _require_int(meta.get("tm_duration"), "draft_meta_info.tm_duration", minimum=1)
    if meta_duration_us != timeline_duration_us:
        raise GateFailure("draft_meta_info duration differs from draft_content duration")
    timeline_duration_frames = _frame_boundary(timeline_duration_us, selected_fps, "draft_content.duration")
    if clips[-1].target_end != timeline_duration_frames:
        raise GateFailure(
            f"last video clip ends at {clips[-1].target_end}, draft duration is {timeline_duration_frames} frames"
        )
    captions = _validate_caption_track(caption_track, by_id, arrays, timeline_duration_us)

    media = probe_media(ffprobe, media_path)
    if (media["fps_num"], media["fps_den"]) != (selected_fps, 1):
        raise GateFailure(
            f"source media fps {media['fps_num']}/{media['fps_den']} differs from timeline {selected_fps}/1"
        )
    media_duration_frames = us_to_frame(int(media["duration_us"]), selected_fps)
    if max(clip.source_out for clip in clips) > media_duration_frames:
        raise GateFailure("a source range exceeds the complete source-media duration")
    canvas = _require_dict(content.get("canvas_config"), "draft_content.canvas_config")
    canvas_width = _require_int(canvas.get("width"), "canvas_config.width", minimum=1)
    canvas_height = _require_int(canvas.get("height"), "canvas_config.height", minimum=1)
    if (canvas_width, canvas_height) != (material_width, material_height):
        raise GateFailure("canvas and video-material dimensions differ")
    if (canvas_width, canvas_height) != (int(media["width"]), int(media["height"])):
        raise GateFailure("canvas dimensions differ from the source media")

    if provenance is not None:
        provenance_timeline = _require_dict(provenance.get("timeline"), "provenance.timeline")
        if provenance_timeline.get("fps") != selected_fps:
            raise GateFailure("provenance frame rate differs from the current draft")
        if (
            provenance_timeline.get("width") != canvas_width
            or provenance_timeline.get("height") != canvas_height
        ):
            raise GateFailure("provenance canvas differs from the current draft")
        provenance_media = _require_dict(
            _require_dict(provenance.get("source"), "provenance.source").get("media"),
            "provenance.source.media",
        )
        if Path(str(provenance_media.get("path", ""))).resolve() != media_path.resolve():
            raise GateFailure("provenance source-media path differs from the current draft")
        expected_fingerprint = _require_dict(
            provenance_media.get("sampled_fingerprint"),
            "provenance.source.media.sampled_fingerprint",
        )
        current_fingerprint = sampled_media_fingerprint(media_path)
        if (
            current_fingerprint.get("algorithm") != expected_fingerprint.get("algorithm")
            or current_fingerprint.get("digest") != expected_fingerprint.get("digest")
            or current_fingerprint.get("size_bytes") != expected_fingerprint.get("size_bytes")
        ):
            raise GateFailure("the source media differs from the provenance fingerprint")

    material_duration_frames = sorted(
        {us_to_frame(int(item["duration"]), selected_fps) for item in arrays["videos"]}
    )
    if any(abs(value - media_duration_frames) > 1 for value in material_duration_frames):
        raise GateFailure(
            f"video material duration differs from probed full duration: {material_duration_frames} vs {media_duration_frames}"
        )

    draft_name = str(meta.get("draft_name") or draft_path.name)
    original_sequence_name = provenance_status.get("original_sequence_name")
    resolved_sequence_name = sequence_name or (
        str(original_sequence_name) if original_sequence_name else draft_name
    )
    ir = ReverseIR(
        draft_path=str(draft_path),
        draft_name=draft_name,
        timeline_id=timeline_id,
        sequence_name=resolved_sequence_name,
        fps=selected_fps,
        fps_source=fps_source,
        width=canvas_width,
        height=canvas_height,
        duration_frames=timeline_duration_frames,
        media_path=str(media_path),
        media_duration_frames=media_duration_frames,
        media_duration_us=int(media["duration_us"]),
        audio_channels=int(media["audio_channels"]),
        audio_sample_rate=int(media["audio_sample_rate"]),
        video_track_id=str(video_track["id"]),
        caption_track_id=str(caption_track["id"]) if caption_track else None,
        clips=clips,
        captions=captions,
        identity_aux_counts=dict(sorted(aux_counts.items())),
        material_duration_frames=material_duration_frames,
    )
    edit_diff = _build_edit_diff(provenance, clips)
    analysis = {
        "status": "passed",
        "draft_path": str(draft_path),
        "jianying_install_dir": str(install_dir),
        "jianying_build": calibrated_build.version,
        "videoeditor_dll_sha256": calibrated_build.dll_sha256,
        "ffprobe": str(ffprobe),
        "root_timeline_content_equal": True,
        "draft_content_version": content.get("new_version") or content.get("version"),
        "app_version": _require_dict(content.get("platform", {}), "platform").get("app_version"),
        "fps": selected_fps,
        "fps_source": fps_source,
        "video_track_count": 1,
        "caption_track_count": 1 if caption_track else 0,
        "video_segments": len(clips),
        "caption_segments": len(captions),
        "duration_frames": timeline_duration_frames,
        "source_media": str(media_path),
        "source_media_frames": media_duration_frames,
        "source_media_duration_us": int(media["duration_us"]),
        "canvas": [canvas_width, canvas_height],
        "audio_channels": int(media["audio_channels"]),
        "audio_sample_rate": int(media["audio_sample_rate"]),
        "material_duration_frames": material_duration_frames,
        "identity_aux_counts": dict(sorted(aux_counts.items())),
        "source_gap_frames": [clips[i + 1].source_in - clips[i].source_out for i in range(len(clips) - 1)],
        "unsupported_or_dropped_elements": 0,
        "provenance": provenance_status,
        "edit_diff": edit_diff,
        "semantic_sha256": _timeline_semantic_sha256(ir),
    }
    return ir, analysis


def _add_text(parent: ET.Element, tag: str, value: Any) -> ET.Element:
    node = ET.SubElement(parent, tag)
    node.text = str(value)
    return node


def _add_rate(parent: ET.Element, fps: int) -> ET.Element:
    rate = ET.SubElement(parent, "rate")
    _add_text(rate, "timebase", fps)
    _add_text(rate, "ntsc", "FALSE")
    return rate


def _pathurl(path: Path) -> str:
    path = path.resolve()
    posix = path.as_posix()
    if posix.startswith("//"):
        parts = posix[2:].split("/", 1)
        host = parts[0]
        tail = "/" + parts[1] if len(parts) > 1 else "/"
        return f"file://{host}{urllib.parse.quote(tail, safe='/:')}"
    return "file://localhost/" + urllib.parse.quote(posix, safe="/:")


def _add_timecode(parent: ET.Element, fps: int) -> None:
    timecode = ET.SubElement(parent, "timecode")
    _add_text(timecode, "string", "00:00:00:00")
    _add_text(timecode, "frame", 0)
    _add_text(timecode, "displayformat", "NDF")
    _add_rate(timecode, fps)


def _add_file_definition(parent: ET.Element, ir: ReverseIR, file_id: str) -> ET.Element:
    media_path = Path(ir.media_path)
    file_node = ET.SubElement(parent, "file", {"id": file_id})
    _add_text(file_node, "duration", ir.media_duration_frames)
    _add_rate(file_node, ir.fps)
    _add_text(file_node, "name", media_path.name)
    _add_text(file_node, "pathurl", _pathurl(media_path))
    _add_timecode(file_node, ir.fps)
    media = ET.SubElement(file_node, "media")
    video = ET.SubElement(media, "video")
    _add_text(video, "duration", ir.media_duration_frames)
    video_sc = ET.SubElement(video, "samplecharacteristics")
    _add_rate(video_sc, ir.fps)
    _add_text(video_sc, "width", ir.width)
    _add_text(video_sc, "height", ir.height)
    _add_text(video_sc, "anamorphic", "FALSE")
    _add_text(video_sc, "pixelaspectratio", "square")
    _add_text(video_sc, "fielddominance", "none")
    audio = ET.SubElement(media, "audio")
    _add_text(audio, "channelcount", ir.audio_channels)
    audio_sc = ET.SubElement(audio, "samplecharacteristics")
    _add_text(audio_sc, "depth", 16)
    _add_text(audio_sc, "samplerate", ir.audio_sample_rate)
    return file_node


def _add_source_track(parent: ET.Element, media_type: str) -> None:
    source_track = ET.SubElement(parent, "sourcetrack")
    _add_text(source_track, "mediatype", media_type)
    _add_text(source_track, "trackindex", 1)


def _add_links(parent: ET.Element, video_id: str, audio_id: str, clip_index: int) -> None:
    for reference, media_type in ((video_id, "video"), (audio_id, "audio")):
        link = ET.SubElement(parent, "link")
        _add_text(link, "linkclipref", reference)
        _add_text(link, "mediatype", media_type)
        _add_text(link, "trackindex", 1)
        _add_text(link, "clipindex", clip_index)
        if media_type == "audio":
            _add_text(link, "groupindex", 1)


def _add_clipitem(
    track: ET.Element,
    ir: ReverseIR,
    clip: ReverseClip,
    *,
    media_type: str,
    file_id: str,
    define_file: bool,
) -> None:
    index = clip.index + 1
    video_id = f"video-clip-{index:04d}"
    audio_id = f"audio-clip-{index:04d}"
    clip_id = video_id if media_type == "video" else audio_id
    item = ET.SubElement(track, "clipitem", {"id": clip_id})
    _add_text(item, "name", Path(ir.media_path).name)
    _add_text(item, "duration", ir.media_duration_frames)
    _add_rate(item, ir.fps)
    _add_text(item, "start", clip.target_start)
    _add_text(item, "end", clip.target_end)
    _add_text(item, "enabled", "TRUE")
    _add_text(item, "in", clip.source_in)
    _add_text(item, "out", clip.source_out)
    if define_file:
        _add_file_definition(item, ir, file_id)
    else:
        ET.SubElement(item, "file", {"id": file_id})
    _add_source_track(item, media_type)
    if media_type == "video":
        _add_text(item, "compositemode", "normal")
    _add_links(item, video_id, audio_id, index)
    ET.SubElement(item, "comments")


def build_fcp7_xml(ir: ReverseIR) -> bytes:
    root = ET.Element("xmeml", {"version": "5"})
    sequence = ET.SubElement(root, "sequence", {"id": "sequence-1"})
    _add_text(sequence, "name", ir.sequence_name)
    _add_text(sequence, "duration", ir.duration_frames)
    _add_rate(sequence, ir.fps)
    _add_text(sequence, "in", -1)
    _add_text(sequence, "out", -1)
    _add_timecode(sequence, ir.fps)
    media = ET.SubElement(sequence, "media")

    video = ET.SubElement(media, "video")
    video_format = ET.SubElement(video, "format")
    video_sc = ET.SubElement(video_format, "samplecharacteristics")
    _add_rate(video_sc, ir.fps)
    _add_text(video_sc, "width", ir.width)
    _add_text(video_sc, "height", ir.height)
    _add_text(video_sc, "anamorphic", "FALSE")
    _add_text(video_sc, "pixelaspectratio", "square")
    _add_text(video_sc, "fielddominance", "none")
    video_track = ET.SubElement(video, "track")
    file_id = "source-file-1"
    for clip in ir.clips:
        _add_clipitem(
            video_track,
            ir,
            clip,
            media_type="video",
            file_id=file_id,
            define_file=(clip.index == 0),
        )
    _add_text(video_track, "enabled", "TRUE")
    _add_text(video_track, "locked", "FALSE")

    audio = ET.SubElement(media, "audio")
    audio_format = ET.SubElement(audio, "format")
    audio_sc = ET.SubElement(audio_format, "samplecharacteristics")
    _add_text(audio_sc, "depth", 16)
    _add_text(audio_sc, "samplerate", ir.audio_sample_rate)
    audio_track = ET.SubElement(audio, "track")
    for clip in ir.clips:
        _add_clipitem(
            audio_track,
            ir,
            clip,
            media_type="audio",
            file_id=file_id,
            define_file=False,
        )
    _add_text(audio_track, "enabled", "TRUE")
    _add_text(audio_track, "locked", "FALSE")

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="utf-8", short_empty_elements=True)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + b"\n"


def _us_to_ms(value_us: int) -> int:
    return (value_us + 500) // 1000


def _format_srt_time(value_ms: int) -> str:
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def build_srt(cues: Sequence[CaptionCue]) -> bytes:
    if not cues:
        raise GateFailure("--output-srt was requested but the draft has no caption track")
    blocks: List[str] = []
    previous_end = 0
    for index, cue in enumerate(cues, start=1):
        start_ms = _us_to_ms(cue.start_us)
        end_ms = _us_to_ms(cue.end_us)
        if start_ms < previous_end or end_ms <= start_ms:
            raise GateFailure(f"caption {index} becomes invalid after millisecond conversion")
        blocks.append(
            f"{index}\r\n{_format_srt_time(start_ms)} --> {_format_srt_time(end_ms)}\r\n{cue.text}"
        )
        previous_end = end_ms
    return b"\xef\xbb\xbf" + ("\r\n\r\n".join(blocks) + "\r\n").encode("utf-8")


def _validate_xml_bytes(xml_bytes: bytes, ir: ReverseIR, parent: Path) -> Dict[str, Any]:
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="._jy_reverse_validate_", dir=str(parent)) as temp_dir:
        temp_path = Path(temp_dir) / "validation.xml"
        temp_path.write_bytes(xml_bytes)
        parsed = parse_fcp7_xml(temp_path)
        expected_ranges = [
            (clip.target_start, clip.target_end, clip.source_in, clip.source_out) for clip in ir.clips
        ]
        parsed_ranges = [
            (clip.target_start, clip.target_end, clip.source_in, clip.source_out) for clip in parsed.clips
        ]
        if parsed_ranges != expected_ranges:
            raise GateFailure("FCP7 round-trip clip ranges differ from the Jianying IR")
        if [
            (clip.target_start, clip.target_end, clip.source_in, clip.source_out)
            for clip in parsed.audio_clips
        ] != expected_ranges:
            raise GateFailure("FCP7 audio round-trip ranges differ from the Jianying IR")
        if parsed.fps != ir.fps or parsed.duration_frames != ir.duration_frames:
            raise GateFailure("FCP7 round-trip rate or duration differs")
        if Path(parsed.media_path).resolve() != Path(ir.media_path).resolve():
            raise GateFailure("FCP7 round-trip media path differs")
        if parsed.xml_media_duration_frames != ir.media_duration_frames:
            raise GateFailure("FCP7 file duration is not the complete source-media duration")

    root = ET.fromstring(xml_bytes.split(b"<!DOCTYPE xmeml>\n", 1)[1])
    video_items = root.findall("./sequence/media/video/track/clipitem")
    audio_items = root.findall("./sequence/media/audio/track/clipitem")
    if len(video_items) != len(ir.clips) or len(audio_items) != len(ir.clips):
        raise GateFailure("FCP7 link validation clip counts differ")
    for index, (video, audio) in enumerate(zip(video_items, audio_items), start=1):
        expected_video = f"video-clip-{index:04d}"
        expected_audio = f"audio-clip-{index:04d}"
        for item, media_type in ((video, "video"), (audio, "audio")):
            source_track = item.find("sourcetrack")
            if source_track is None:
                raise GateFailure(f"clip {index} is missing sourcetrack")
            if source_track.findtext("mediatype") != media_type or source_track.findtext("trackindex") != "1":
                raise GateFailure(f"clip {index} has incorrect sourcetrack")
            refs = [node.findtext("linkclipref") for node in item.findall("link")]
            if refs != [expected_video, expected_audio]:
                raise GateFailure(f"clip {index} has incorrect video/audio links")
    return {
        "parsed": True,
        "xmeml_version": root.attrib.get("version"),
        "video_clips": len(video_items),
        "audio_clips": len(audio_items),
        "paired_links": len(video_items),
        "source_tracks": len(video_items) + len(audio_items),
        "unsupported_or_dropped_elements": 0,
    }


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise FileExistsError(f"output already exists; refusing to overwrite: {path}") from None


def reverse_draft(
    draft_path: Path,
    output_xml: Path,
    *,
    output_srt: Optional[Path] = None,
    report_path: Optional[Path] = None,
    jianying_install_dir: Optional[Path] = None,
    ffprobe_path: Optional[Path] = None,
    fps: Optional[int] = None,
    sequence_name: Optional[str] = None,
) -> Dict[str, Any]:
    output_xml = output_xml.resolve()
    output_srt = output_srt.resolve() if output_srt is not None else None
    report_path = (
        report_path.resolve()
        if report_path is not None
        else output_xml.with_name(output_xml.stem + ".report.json")
    )
    destinations = [output_xml, report_path] + ([output_srt] if output_srt is not None else [])
    if len(set(destinations)) != len(destinations):
        raise GateFailure("output XML, SRT, and report paths must be distinct")
    existing = [str(path) for path in destinations if path.exists()]
    if existing:
        raise FileExistsError(f"output exists; refusing to overwrite: {existing}")

    ir, analysis = analyze_draft(
        draft_path,
        jianying_install_dir=jianying_install_dir,
        ffprobe_path=ffprobe_path,
        fps=fps,
        sequence_name=sequence_name,
    )
    xml_bytes = build_fcp7_xml(ir)
    xml_verification = _validate_xml_bytes(xml_bytes, ir, output_xml.parent)
    srt_bytes = build_srt(ir.captions) if output_srt is not None else None

    report: Dict[str, Any] = {
        **analysis,
        "status": "passed",
        "output_xml": str(output_xml),
        "output_srt": str(output_srt) if output_srt is not None else None,
        "report_path": str(report_path),
        "xml_sha256": hashlib.sha256(xml_bytes).hexdigest(),
        "srt_sha256": hashlib.sha256(srt_bytes).hexdigest() if srt_bytes is not None else None,
        "xml_verification": xml_verification,
        "caption_export": {
            "requested": output_srt is not None,
            "cue_count": len(ir.captions),
            "single_line": all(not re.search(r"[\r\n]", cue.text) for cue in ir.captions),
        },
        "gates": {
            "encrypted_root_and_timeline_decoded_equal": True,
            "one_video_track": True,
            "at_most_one_caption_track": True,
            "single_source_media": True,
            "video_timeline_continuous": True,
            "speed_1x": True,
            "identity_visual_transform": True,
            "unsupported_effects": 0,
            "unknown_tracks_or_materials": 0,
            "v1_a1_pairs": len(ir.clips),
        },
    }
    report_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    _write_new(output_xml, xml_bytes)
    if output_srt is not None and srt_bytes is not None:
        _write_new(output_srt, srt_bytes)
    _write_new(report_path, report_bytes)
    return report


def _safe_output_stem(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:100] or "剪映时间线"


def reverse_bundle(
    draft_path: Path,
    output_dir: Path,
    *,
    include_srt: bool = True,
    jianying_install_dir: Optional[Path] = None,
    ffprobe_path: Optional[Path] = None,
    fps: Optional[int] = None,
    sequence_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create one atomically committed export directory for the formal reverse route."""

    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output bundle exists; refusing to overwrite: {output_dir}")
    if not output_dir.parent.is_dir():
        raise FileNotFoundError(f"output bundle parent does not exist: {output_dir.parent}")

    ir, analysis = analyze_draft(
        draft_path,
        jianying_install_dir=jianying_install_dir,
        ffprobe_path=ffprobe_path,
        fps=fps,
        sequence_name=sequence_name,
        require_provenance=True,
    )
    xml_bytes = build_fcp7_xml(ir)
    srt_bytes = build_srt(ir.captions) if include_srt and ir.captions else None
    stem = _safe_output_stem(ir.draft_name)
    xml_name = f"{stem}｜JY回导｜V01.xml"
    srt_name = f"{stem}｜JY回导｜V01.srt" if srt_bytes is not None else None
    receipt_name = "export_receipt.json"
    staging = output_dir.parent / f"._timeline_bridge_export_{uuid.uuid4().hex}"
    if staging.exists():
        raise FileExistsError(f"unexpected export staging collision: {staging}")

    try:
        staging.mkdir()
        xml_verification = _validate_xml_bytes(xml_bytes, ir, staging)
        xml_path = staging / xml_name
        xml_path.write_bytes(xml_bytes)
        srt_verification: Optional[Dict[str, Any]] = None
        if srt_bytes is not None and srt_name is not None:
            srt_path = staging / srt_name
            srt_path.write_bytes(srt_bytes)
            parsed_cues = parse_srt(srt_path)
            expected_cues = [
                (_us_to_ms(cue.start_us), _us_to_ms(cue.end_us), cue.text) for cue in ir.captions
            ]
            actual_cues = [(cue.start_ms, cue.end_ms, cue.text) for cue in parsed_cues]
            if actual_cues != expected_cues:
                raise GateFailure("SRT round-trip differs from the Jianying caption projection")
            long_caption_count = sum(len(cue.text) > 24 for cue in ir.captions)
            srt_verification = {
                "parsed": True,
                "cue_count": len(parsed_cues),
                "single_line": True,
                "longer_than_24_visible_characters": long_caption_count,
                "precision_status": "passed" if long_caption_count == 0 else "review_required",
            }

        report: Dict[str, Any] = {
            **analysis,
            "status": "passed",
            "formal_managed_export": True,
            "output_bundle": str(output_dir),
            "output_xml": str(output_dir / xml_name),
            "output_srt": str(output_dir / srt_name) if srt_name is not None else None,
            "report_path": str(output_dir / receipt_name),
            "xml_sha256": hashlib.sha256(xml_bytes).hexdigest(),
            "srt_sha256": hashlib.sha256(srt_bytes).hexdigest() if srt_bytes is not None else None,
            "xml_verification": xml_verification,
            "srt_verification": srt_verification,
            "gates": {
                "managed_provenance": True,
                "calibrated_jianying_build": True,
                "stable_encrypted_snapshot": True,
                "one_video_track": True,
                "at_most_one_caption_track": True,
                "single_source_media": True,
                "video_timeline_continuous": True,
                "speed_1x": True,
                "identity_visual_transform": True,
                "unsupported_effects": 0,
                "unknown_tracks_or_materials": 0,
                "v1_a1_pairs": len(ir.clips),
            },
        }
        receipt_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (staging / receipt_name).write_bytes(receipt_bytes)
        if hashlib.sha256(xml_path.read_bytes()).hexdigest() != report["xml_sha256"]:
            raise GateFailure("staged XML hash differs before export commit")
        if srt_name is not None and hashlib.sha256((staging / srt_name).read_bytes()).hexdigest() != report["srt_sha256"]:
            raise GateFailure("staged SRT hash differs before export commit")
        if output_dir.exists():
            raise FileExistsError(f"output bundle appeared during export: {output_dir}")
        os.replace(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strict Jianying 11.2 draft to DaVinci-readable FCP7/xmeml v5 converter"
    )
    parser.add_argument("--draft", required=True, type=Path, help="Jianying draft directory")
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        "--output-bundle", type=Path, help="new atomically committed formal export directory"
    )
    output_group.add_argument("--output-xml", type=Path, help="legacy standalone FCP7/xmeml v5 output path")
    parser.add_argument("--output-srt", type=Path, help="optional new single-line UTF-8-BOM SRT path")
    parser.add_argument("--report", type=Path, help="optional new JSON report path")
    parser.add_argument("--no-srt", action="store_true", help="omit SRT from a formal output bundle")
    parser.add_argument("--jianying-install-dir", type=Path, help="directory containing JianyingPro.exe")
    parser.add_argument("--ffprobe", type=Path, help="ffprobe executable; auto-detected when omitted")
    parser.add_argument("--fps", type=int, help="integer timeline fps; draft value wins, otherwise defaults to 30")
    parser.add_argument("--sequence-name", help="optional FCP7 sequence name")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output_bundle is not None:
            if args.output_srt is not None or args.report is not None:
                raise GateFailure("--output-srt/--report cannot be combined with --output-bundle")
            report = reverse_bundle(
                args.draft,
                args.output_bundle,
                include_srt=not args.no_srt,
                jianying_install_dir=args.jianying_install_dir,
                ffprobe_path=args.ffprobe,
                fps=args.fps,
                sequence_name=args.sequence_name,
            )
        else:
            if args.no_srt:
                raise GateFailure("--no-srt is only valid with --output-bundle")
            report = reverse_draft(
                args.draft,
                args.output_xml,
                output_srt=args.output_srt,
                report_path=args.report,
                jianying_install_dir=args.jianying_install_dir,
                ffprobe_path=args.ffprobe,
                fps=args.fps,
                sequence_name=args.sequence_name,
            )
    except (
        GateFailure,
        FileExistsError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        ET.ParseError,
    ) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "passed",
                "output_xml": report["output_xml"],
                "output_srt": report["output_srt"],
                "report": report["report_path"],
                "clips": report["video_segments"],
                "captions": report["caption_segments"],
                "fps": report["fps"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
