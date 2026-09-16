# Protocol conformance fixtures

The conformance suite will verify:

- canonical JSON and DID signatures;
- registration challenge single-use and expiry;
- signed request replay protection;
- task claim races and lease expiry;
- proof/result hashes;
- immutable validation decisions;
- full/partial/refund mock escrow transitions;
- activity eligibility separation from mock activity.

The current MVP includes integration coverage under `tests/`. The verification map is in `docs/AUDIT_VERIFICATION.md`. Expand these fixtures before federation or real-token settlement.
