# Tutorial screenshot provenance

The JPEG files in this directory are stable documentation copies of selected
frames from the checksummed July 13, 2026 Oxcart human-workflow validation.
They are live application captures, not mockups or reconstructed terminal art.

The validation ran source commit
`cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7` with Oxcart acting as both the
controller and target through Vela's implicit `local` transport. Hostnames,
paths, ports, model IDs, revisions, and image digests therefore document that
specific proof and are examples for readers—not portable defaults.

[`manifest.json`](manifest.json) records every stable filename, original
evidence path, SHA-256 digest, byte size, evidence-session start, and
reader-facing purpose. The source pack does not retain a per-frame capture
timestamp. Publish or verify the corpus from the repository root with:

```bash
python3 scripts/sync_docs_screenshots.py
python3 scripts/sync_docs_screenshots.py --check
```

The sync is byte-for-byte. It does not resize, annotate, or re-encode images.
Frames whose visible state did not honestly match their filename—including two
READY-like stop captures, one render-degraded stop capture, and a purported
save-controls frame that did not show those controls—are intentionally excluded.
Full-canvas responsive captures remain in the evidence pack but are not
published as reader assets because their UI text is too small at documentation
width. The clean `25a-run1-stop-check.jpg` source is the tutorial's STOPPED proof.

When replacing the walkthrough, first produce and validate a new checksummed
evidence pack, update the explicit selection and runtime commit in
`scripts/sync_docs_screenshots.py`, regenerate this directory, and run the
documentation and secret-scan gates before commit.
