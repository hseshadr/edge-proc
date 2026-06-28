# Security Policy

EdgeProc is a **signing- and verification-adjacent** library: it mints trust-root keys,
signs version pointers, and verifies content-addressed bundles fail-closed before promoting
them. We treat cryptographic and supply-chain issues with the seriousness that posture
demands. If a verification, signature, content-addressing, or trust-pinning path can be
bypassed, weakened, or made to silently accept tampered data, that is a high-severity bug.

## Reporting a vulnerability

**Please do not open a public GitHub issue, pull request, or discussion for a security
vulnerability** — that discloses it before a fix exists.

Instead, report it privately by email to:

> **harish.seshadri@gmail.com**

Please include, as far as you can:

- A description of the issue and the impact (what a malicious publisher / CDN / network
  attacker could achieve).
- The affected version(s) or commit.
- Steps to reproduce, or a minimal proof of concept.
- Any suggested remediation.

You can expect an acknowledgement within a few days. We will work with you to confirm the
issue, develop a fix, and coordinate disclosure. We are happy to credit you in the release
notes unless you prefer to remain anonymous.

## Supported versions

EdgeProc is pre-1.0 (alpha). Security fixes land on `main` and ship in the next release. We
support the **latest released version**; please upgrade to it before reporting, in case the
issue is already fixed.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Scope notes

- The trust model is **pinned-key, fail-closed**: a consumer trusts only a pubkey it pins
  out-of-band; `sync` refuses to run without one. Reports that demonstrate trust without a
  pinned key, signature acceptance after tampering, or content-address bypass are in scope
  and prioritized.
- Issues in third-party dependencies (e.g. `cryptography`, `faiss-cpu`) should be reported
  upstream; if EdgeProc's *use* of them is the weakness, report it here.
