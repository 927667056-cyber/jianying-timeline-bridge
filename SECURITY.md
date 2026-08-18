# Security policy

## Supported version

Only the latest tagged alpha receives fixes. Compatibility is bound to the exact Jianying build and DLL hash listed in `COMPATIBILITY.md`.

## Safe reporting

Use a private GitHub Security Advisory for vulnerabilities. Do not attach raw Jianying drafts, `root_meta_info.json`, media, conversion receipts, operation locks, or system logs until they have been independently reviewed and sanitized.

Potentially sensitive fields include home paths, draft-store paths, device IDs, MAC addresses, disk identifiers, account identifiers, and source-media metadata.

## Local DLL boundary

The bridge does not distribute Jianying or `videoeditor.dll`. It invokes an exact, locally installed DLL in an isolated child process and refuses any unknown hash. Process isolation limits Python-process crashes; it is not a general security sandbox.

Never bypass the build fingerprint gate or point the bridge at a DLL obtained from an untrusted source.
