# Compatibility profile

## Supported in 0.1.1-alpha.1

- Windows x64 and 64-bit Python 3.10 or newer.
- Jianying Pro build `11.2.0.14339`.
- `videoeditor.dll` SHA-256 `654371a6dca840f53ae786df43fc345365e54d2189d5572ce7c4658aab23f540`.
- FCP7/xmeml version 5, 30fps constant-frame-rate, non-drop timeline.
- One original video source with embedded audio.
- One continuous V1/A1 pair with strictly linked and paired clips.
- Normal speed and identity transforms/effects.
- Optional one-line SRT captions.
- Source-media duration must match exactly, except that the measured file may contain one additional trailing frame which no XML clip uses.
- Formal reverse export only from a draft created and tracked by this bridge.

## Deliberately rejected

- Unknown Jianying versions or a changed DLL hash.
- Multiple source files or extra populated audio/video tracks.
- Nested, compound, or multicam timelines.
- Speed changes, transitions, non-identity effects, transforms, or keyframes.
- Gaps, overlaps, offline media, or changed media identity.
- Media shorter than declared, more than one frame longer than declared, or a clip extending past the XML-declared media boundary.
- Unknown populated fields that cannot be proved harmless.
- Unmanaged Jianying drafts without valid bridge provenance.
- Existing output names or unfinished deployment transactions.

These restrictions are part of the safety design. A capability should be added only with a dedicated fixture, negative gates, a real Jianying open/save/reopen round trip, and an updated exact compatibility profile.
