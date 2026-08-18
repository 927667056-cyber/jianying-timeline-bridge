# Privacy

The bridge runs locally and contains no telemetry, analytics, cloud upload, or network client.

Conversion reports and provenance intentionally record file identities and paths so a reverse conversion can prove that it is reading the expected media and draft. Those local artifacts can therefore contain sensitive information even though nothing is transmitted automatically.

Before sharing a bug report:

1. Do not upload source media or a real customer draft.
2. Do not upload `root_meta_info.json`.
3. Remove user names, home paths, draft-store paths, media names, device identifiers, and project text.
4. Reproduce the problem with a synthetic project whenever possible.
5. Review every file in the bundle manually before publishing it.

The repository's bundled profile was rebuilt from a synthetic template, decrypted for inspection, stripped of backups and provenance, normalized, re-encrypted, and scanned for known private patterns.

Maintainer commits must use a GitHub-provided `noreply` address. Release documentation uses ranges rather than exact private-program measurements. Every release must be built from the manifest allowlist and pass `scripts/privacy_audit.py`; the encrypted template must additionally be decrypted and inspected on the supported local Jianying build before publication.
