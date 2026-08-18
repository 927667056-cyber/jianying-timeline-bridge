# Verified compatibility profile

## Supported in version 0.1.1

- Windows x64.
- Jianying Pro build `11.2.0.14339`.
- `videoeditor.dll` SHA-256 `654371a6dca840f53ae786df43fc345365e54d2189d5572ce7c4658aab23f540`.
- FCP7/xmeml version 5, 30 fps constant-frame-rate, non-drop timeline.
- One original video source with embedded audio.
- One continuous V1/A1 pair, clips strictly linked and paired, normal speed, identity transforms and effects.
- Optional single-line SRT captions.
- Source-media duration must match exactly, except for one additional measured trailing frame that is outside every XML clip range.
- Formal reverse export only from a draft created and tracked by this bridge.

## Deliberately rejected

- Unknown Jianying versions or a changed DLL hash.
- Multiple source media files, additional populated audio/video tracks, nested or multicam timelines.
- Speed changes, transitions, non-identity effects, transforms, keyframes, compound clips, gaps, overlaps, or unknown populated fields.
- Offline or identity-mismatched source media.
- Media shorter than declared, more than one frame longer than declared, or any source range beyond the XML-declared media boundary.
- Unmanaged Jianying drafts without bridge provenance.
- Existing output names and unfinished deployment transactions.

These restrictions prevent silent damage. Add a capability only after a dedicated fixture, negative gates, a real application round trip, and a new exact compatibility profile pass.
