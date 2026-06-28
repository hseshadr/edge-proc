---
name: Bug report
about: Report a reproducible problem in EdgeProc
title: "[Bug] "
labels: bug
assignees: ""
---

**Describe the bug**
A clear and concise description of what the bug is.

**To reproduce**
Steps or a minimal snippet / CLI invocation that triggers it:

```bash
# e.g. edgeproc sync --base-url origin --cache-dir cache --key keys/public.key
```

**Expected behavior**
What you expected to happen.

**Actual behavior**
What actually happened (include the full error / traceback and exit code).

**Environment**
- EdgeProc version (`edgeproc version`):
- Python version (`python --version`, should be 3.13+):
- OS:
- Extras installed (`localvec`, `bundles`, both, or core only):

**Additional context**
Anything else that helps — config, `EDGEPROC_`-prefixed env vars, sample task/bundle.

> ⚠️ For **security vulnerabilities**, do NOT file a public issue — see [SECURITY.md](../../SECURITY.md).
