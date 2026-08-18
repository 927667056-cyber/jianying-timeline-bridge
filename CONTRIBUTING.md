# Contributing

Bug reports and compatibility research are welcome, but privacy and fail-closed behavior take priority over feature breadth.

- Reproduce with synthetic media and timelines.
- Never attach a raw user draft or draft-store index.
- Add a positive fixture and a negative gate for every new supported structure.
- Do not weaken exact version, media identity, provenance, or no-overwrite checks.
- Do not add or bundle Jianying DLLs, installers, keys, or copyrighted user media.
- Keep public tests deterministic and independent of private machine paths.

Compatibility for a new Jianying build requires an exact DLL fingerprint, encrypted write/read-back verification, application open/save/reopen acceptance, and a dedicated version profile.
