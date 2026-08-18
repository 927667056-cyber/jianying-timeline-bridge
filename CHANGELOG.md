# Changelog

## 0.1.1-alpha.1 — 2026-08-18

- Accept an FCP7 source-media duration that is exactly one frame shorter than the measured file only when that additional measured frame is an unused trailing frame.
- Keep rejecting shorter media, differences larger than one frame, and any source range outside the XML-declared media boundary.
- Record the duration compatibility policy and both frame counts in the gate report.
- Expand public negative-gate tests from 7 to 13 cases.

## 0.1.0-alpha.1 — 2026-08-17

- First public, privacy-sanitized alpha.
- Added strict FCP7 XML/SRT to Jianying 11.2 conversion.
- Added managed Jianying draft to deterministic FCP7 XML/SRT export.
- Added exact build fingerprinting, no-overwrite behavior, provenance, operation locks, transaction journal, index verification, and recovery.
- Added a rebuilt 27-file synthetic encrypted profile with no customer media, real project paths, device index, backups, or conversion provenance.
- Added a Codex Skill integration and public smoke tests.
