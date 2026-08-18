---
name: jianying-timeline-bridge
description: Safely convert a supported DaVinci Resolve or Premiere FCP7 XML timeline plus optional SRT into an editable Jianying Pro 11.2 draft, or convert a bridge-managed Jianying draft back into deterministic FCP7 XML and precise one-line SRT. Use for timeline-to-Jianying, Jianying-to-timeline, source-handle preservation, compatibility checks, round-trip validation, or recovery of an interrupted bridge deployment.
---

# Jianying Timeline Bridge

Use the installed Jianying Timeline Bridge software as the only conversion engine. This skill selects the workflow, runs safety gates, explains failures, and checks the receipts; it must not reimplement or bypass the engine.

## Required workflow

1. Read [supported-profile.md](references/supported-profile.md) before a conversion.
2. Locate the installed software with `scripts/run_bridge.py doctor`.
3. Require Jianying Pro to be fully closed before reading, preparing, deploying, recovering, or exporting a draft.
4. Choose one formal route:
   - FCP7 XML and optional SRT to Jianying: `to-jianying`.
   - Bridge-managed Jianying draft to FCP7 XML and SRT: `from-jianying`.
   - Interrupted deployment inspection and recovery: `recover`.
5. Use a new output name or directory. Never overwrite, replace, merge into, or delete an existing draft or export.
6. Read the generated receipt and report the gate status, segment count, caption count, duration, media identity, supported build, and output path.

Run the wrapper with the current Python interpreter; it only locates the installed software and forwards arguments without reproducing conversion logic. Use these command shapes:

```text
python scripts/run_bridge.py doctor
python scripts/run_bridge.py to-jianying --xml <input.xml> [--srt <input.srt>] --draft-name <new-name> --run-dir <new-run-dir>
python scripts/run_bridge.py to-jianying --xml <input.xml> [--srt <input.srt>] --draft-name <new-name> --run-dir <new-run-dir> --deploy
python scripts/run_bridge.py from-jianying --draft <managed-draft-dir> --output-bundle <new-output-dir> [--no-srt]
python scripts/run_bridge.py recover --report-dir <new-report-dir>
```

## Forward conversion

- Accept only an FCP7/xmeml v5 XML that passes every profile gate.
- Preserve original-media references and each clip's source in/out; do not substitute a flattened reference movie.
- Pass an SRT explicitly when it does not share the XML basename.
- Let the software use its fingerprinted clean profile template. Never choose an arbitrary user draft as a template.
- Prefer prepare-only when inspecting a new source. Deploy only after preparation and structural verification pass.
- A draft-name collision is a successful safety stop, not permission to replace the existing draft.

## Reverse conversion

- Use the formal route only for drafts containing valid bridge provenance.
- Treat a draft without provenance as unmanaged. The current formal CLI has no unmanaged-draft export or diagnostic route; report it as unsupported and stop rather than decrypting or interpreting it outside the engine.
- The software must compare provenance, encrypted content, source-media identity, build identity, segment IDs, frame ranges, and timeline continuity before export.
- Export the XML, optional SRT, and receipt together as one new atomic bundle.

## Failure policy

Stop with zero output when the version, DLL fingerprint, media identity, frame rate, track structure, speed, effects, transform, continuity, provenance, or transaction state is unsupported or inconsistent. State the exact gate that stopped. Do not silently flatten, approximate, skip clips, reinterpret unknown fields, or downgrade to a different route.

If a deployment journal is unfinished, run `recover` before retrying. Recovery may quarantine a bridge-created suspect directory for diagnosis; it must not touch unrelated drafts.

## Completion criteria

A forward conversion is complete only when preparation, encrypted round-trip verification, deployment transaction, root-index verification, and receipt archival pass. A reverse conversion is complete only when the atomic export bundle exists and its receipt reports semantic equivalence. Opening the result in Jianying or DaVinci remains a UI acceptance step for a newly calibrated application build.
