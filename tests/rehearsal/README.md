# tests/rehearsal/

What this is: a record-shaped snapshot of a real ASF product, built by `asf rehearse --build`
(`asf/rehearsal.py:build`). Every id is kept verbatim at its real width; every structural
frontmatter field, date and History line's date/verb/id reference is kept; every title, body,
intake note and tag annotation is cleared to deterministic ASCII filler of the same length and
line count. No title, body, History prose, intake note text or commit message from the real
record is in it.

What it must preserve: `manifest.json`'s claims — card count per type, widest id per prefix, the
tag/ref plan, the intake states. `rehearsal.manifest_holds` checks the snapshot against them;
a refresh that changes a claim it does not also meet is a builder defect, not a passing rebuild.

How to refresh it: `asf rehearse --build --from-product <p>`, then re-run this product's own
unit tests and its generic/privacy scans before committing the diff.

What never goes in it: any real title, body, History prose, intake note text, commit message,
account name or machine path. The filler is seeded only by the length of what it replaces.
