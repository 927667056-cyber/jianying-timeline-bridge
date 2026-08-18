# Validation evidence

## Publicly reproducible checks

- Python compilation for the five bridge modules and the Codex Skill wrapper.
- Thirteen public unit tests for frame conversion, off-grid rejection, safe draft names, guarded output paths, SRT serialization, deterministic FCP7 XML construction/parsing, and the exact one-unused-trailing-frame duration policy with negative gates.
- Decryption and JSON inspection of every encrypted file in the bundled clean profile.
- Encrypted encode/decode round trip after privacy normalization.
- Full-tree scan for known user paths, real project names, backup files, device IDs, MAC addresses, and disk identifiers.
- Package manifest with per-file SHA-256 values.

## Private application acceptance

The development acceptance suite covered two independent long-form timelines with:

- Hundreds of paired video/audio edit segments.
- Hundreds of one-line captions.
- More than thirty thousand timeline frames at 30fps.
- Exact target and source boundaries after a complete managed round trip.
- Original-media source handles preserved.
- Two managed edit scenarios: extending a source-backed edit point and ripple deletion.
- Output-collision, extra-track, speed-change, non-identity-transform, provenance-tamper, media-identity, and interrupted-transaction negative gates.
- A separate timeline with more than 450 paired edits whose measured source contained exactly one unused trailing frame beyond the XML declaration.
- Exact sequence name, canvas, media identity, every video/audio target and source range, and every source gap after the managed forward/reverse round trip.
- Rejection tests for shorter media, a two-frame surplus, and a source range outside the XML-declared media boundary.

The exact program scale, private media, real draft, transcript, report paths, and draft index are intentionally not published. These ranges document the tested scale; they are not a claim that arbitrary Jianying projects are supported.
