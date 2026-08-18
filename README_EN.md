# Jianying Timeline Bridge

[简体中文](README.md) · [Compatibility](COMPATIBILITY.md) · [Security](SECURITY.md) · [Privacy](PRIVACY.md)

A fail-closed, frame-accurate bridge between supported FCP7 XML/SRT timelines and encrypted Jianying Pro 11.2 drafts.

The forward route preserves original-media references, full source duration, and every clip's source in/out so edit points retain their available handles. The reverse route exports deterministic FCP7 XML and one-line SRT only from bridge-managed drafts with valid provenance.

## What it does

- FCP7 XML plus optional SRT to an encrypted Jianying Pro 11.2 draft.
- Bridge-managed Jianying draft to deterministic FCP7 XML plus optional SRT.
- Encrypted write/read-back verification and frame-level semantic comparison.
- Exact Jianying build and DLL fingerprint gate.
- No-overwrite outputs, operation locks, provenance, transaction journal, index backup, and recovery.
- Local-only processing with no telemetry or media upload.

## Verified profile

Version `0.1.1-alpha.1` supports Windows x64, Jianying Pro `11.2.0.14339`, FCP7/xmeml v5, 30fps CFR/NDF, one original video source with embedded audio, one continuous paired V1/A1 timeline, normal speed, identity transforms, and optional one-line SRT captions. The measured source may be exactly one frame longer than the XML declaration only when that final frame is unused by every clip.

Unknown versions, multiple sources, extra populated tracks, gaps, overlaps, speed changes, transitions, transforms, effects, nested timelines, identity mismatches, and unmanaged drafts are rejected. See [COMPATIBILITY.md](COMPATIBILITY.md).

## Setup

Install 64-bit Python 3.10+, FFmpeg/FFprobe, and the exact supported Jianying build. This repository does not ship Jianying, vendor DLLs, user drafts, or media.

The easiest setup is to run `00_首次配置.bat` and select the exact Jianying install, `ffmpeg.exe`, `ffprobe.exe`, and the Jianying draft root. It creates a local ignored `software_build/bridge_config.json` and refuses to overwrite an existing configuration.

Alternatively, set these environment variables in one PowerShell session:

```powershell
$env:JYBRIDGE_JIANYING_HOME = 'C:\Path\To\JianyingPro\11.2.0.14339'
$env:JYBRIDGE_FFMPEG = 'C:\Path\To\ffmpeg.exe'
$env:JYBRIDGE_FFPROBE = 'C:\Path\To\ffprobe.exe'
$env:JYBRIDGE_DRAFT_ROOT = 'C:\Path\To\JianyingPro Drafts'
```

Close Jianying completely, then run this in the same PowerShell session:

```powershell
python bridge_cli.py doctor
```

The Chinese GUI can be launched with `启动_剪映时间线双向桥.bat`. CLI command shapes are documented in the [Chinese README](README.md).

## Validation

Private acceptance testing covered two independent long-form timelines with hundreds of paired A/V clips, hundreds of one-line captions, and more than thirty thousand timeline frames. It also exercised the single unused trailing-frame compatibility route. Every target/source boundary matched after a full managed round trip. Exact program scale, real media, drafts, absolute paths, device metadata, and customer reports are intentionally excluded. See [docs/VALIDATION.md](docs/VALIDATION.md).

## Important notice

This is an independent interoperability project and is not affiliated with or endorsed by Jianying, CapCut, ByteDance, or Shenzhen Lianmeng Technology. It invokes only an exact, locally installed, user-provided Jianying component and does not distribute vendor software, keys, or media.

This alpha is deliberately narrow. Always perform an application-level open/save/reopen acceptance check before production use.

## License

Apache License 2.0. The bundled minimal crypto surface is derived from pyJianYingDraft 0.3.0; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
