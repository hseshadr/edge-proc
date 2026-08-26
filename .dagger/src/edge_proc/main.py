"""EdgeProc's complete quality, security, and release-candidate graph."""

from __future__ import annotations

from typing import Final, Self

import dagger
from dagger import check, dag, field, function, object_type

PYTHON_IMAGE: Final = (
    "python:3.13.14-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6"
)
UV_IMAGE: Final = (
    "ghcr.io/astral-sh/uv:0.11.32@sha256:"
    "df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c"
)
ACTIONLINT_IMAGE: Final = (
    "rhysd/actionlint:1.7.10@sha256:"
    "ef8299f97635c4c30e2298f48f30763ab782a4ad2c95b744649439a039421e36"
)
GITLEAKS_IMAGE: Final = (
    "ghcr.io/gitleaks/gitleaks:v8.29.1@sha256:"
    "aa036a2f4bdfe3cc3c55fa4326308efabb4a6be498c883c864fd1d0d5585438a"
)
REPOSITORY: Final = "hseshadr/edge-proc"
REPOSITORY_URL: Final = f"https://github.com/{REPOSITORY}.git"
SHA_LENGTH: Final = 40
SOURCE_EXCLUDES: Final = [
    ".git",
    ".venv",
    ".dagger/.venv",
    ".dagger/sdk",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "**/__pycache__",
    "dist",
]
GITLEAKS_SNAPSHOT: Final = [
    "gitleaks",
    "detect",
    "--source",
    "/snapshot",
    "--no-git",
    "--redact",
    "--no-banner",
]
GITLEAKS_HISTORY: Final = [
    "gitleaks",
    "detect",
    "--source",
    "/repo",
    "--log-opts=--all",
    "--redact",
    "--no-banner",
]


@object_type
class EdgeProc:
    """Run the same typed EdgeProc release graph locally and on GitHub."""

    source: dagger.Directory = field()

    @classmethod
    def create(cls, workspace: dagger.Workspace) -> Self:
        """Construct the graph from an explicit typed workspace snapshot."""
        instance = cls.__new__(cls)
        instance.source = workspace.directory("/", exclude=SOURCE_EXCLUDES)
        return instance

    @function
    def quality(self) -> dagger.Container:
        """Run lint, format, strict typing, Grade A complexity, and 90%+ tests."""
        return self._quality(self.source)

    @function
    def dependency_audit(self) -> dagger.Container:
        """Audit the exact frozen dependency graph without suppressions."""
        return self._dependency_audit(self.source)

    def _dependency_audit(self, source: dagger.Directory) -> dagger.Container:
        export = [
            "uv",
            "export",
            "--frozen",
            "--all-extras",
            "--no-emit-project",
            "--no-hashes",
            "-o",
            "/opt/tmp/audit.txt",
        ]
        audit = [
            "uv",
            "run",
            "pip-audit",
            "-r",
            "/opt/tmp/audit.txt",
            "--disable-pip",
            "--no-deps",
        ]
        return self._python(source).with_exec(export).with_exec(audit)

    @function
    def secret_scan(self, commit_sha: str = "") -> dagger.Container:
        """Scan the exact snapshot and complete canonical history with Gitleaks."""
        return self._secret_scan(self.source, commit_sha)

    def _secret_scan(self, source: dagger.Directory, commit_sha: str = "") -> dagger.Container:
        if commit_sha:
            self._require_sha(commit_sha)
            history = dag.git(REPOSITORY_URL).commit(commit_sha).tree(depth=0, include_tags=True)
        else:
            history = dag.git(REPOSITORY_URL).branch("main").tree(depth=0, include_tags=True)
        scan = self._gitleaks().with_directory("/snapshot", source)
        scan = scan.with_exec(["sh", "-ceu", 'test -n "$(find /snapshot -type f -print -quit)"'])
        scan = scan.with_exec(GITLEAKS_SNAPSHOT).with_directory("/repo", history)
        return scan.with_exec(GITLEAKS_HISTORY)

    @function
    def workflow_security(self) -> dagger.Container:
        """Validate every GitHub ingress workflow with pinned actionlint."""
        return self._workflow_security(self.source)

    def _workflow_security(self, source: dagger.Directory) -> dagger.Container:
        workflows = source.directory(".github/workflows")
        command = (
            "find .github/workflows -type f "
            "\\( -name '*.yml' -o -name '*.yaml' \\) -exec actionlint {} +"
        )
        return (
            self._actionlint()
            .with_directory("/repo/.github/workflows", workflows)
            .with_exec(["sh", "-ceu", command])
        )

    @function
    @check
    async def ci(self, commit_sha: str = "") -> str:
        """Run the canonical release gate sequentially to bound runner memory."""
        await self._run_ci(self.source, commit_sha)
        return "EdgeProc canonical Dagger gate passed"

    async def _run_ci(self, source: dagger.Directory, commit_sha: str = "") -> None:
        tested = self._quality(source).with_exec(["bash", "examples/run_loop.sh"])
        await tested.with_exec(["uv", "run", "python", "benchmarks/benchmark.py"]).sync()
        await self._dependency_audit(source).sync()
        await self._secret_scan(source, commit_sha).sync()
        await self._workflow_security(source).sync()

    @function
    async def release_candidate(
        self, tag: str, commit_sha: str, github_token: dagger.Secret
    ) -> dagger.Directory:
        """Build one exact, Dagger-proven candidate without publishing it."""
        self._require_sha(commit_sha)
        await self._hosted(commit_sha, tag, github_token).sync()
        source = self._release_source(commit_sha)
        await self._identity(source, tag).sync()
        await self._run_ci(source, commit_sha)
        return self._candidate(source, tag).directory("/candidate")

    def _identity(self, source: dagger.Directory, tag: str) -> dagger.Container:
        command = [
            "uv",
            "run",
            "python",
            "scripts/release_contract.py",
            "identity",
            "--root",
            ".",
            "--tag",
            tag,
        ]
        return self._python(source).with_exec(command)

    def _candidate(self, source: dagger.Directory, tag: str) -> dagger.Container:
        built = self._python(source).with_exec(
            ["uv", "build", "--no-build-isolation", "--out-dir", "dist"]
        )
        built = built.with_exec(self._distribution_command(tag))
        built = built.with_exec(self._checksum_command())
        copy = "mkdir /candidate && cp -R dist /candidate/dist && cp SHA256SUMS /candidate/"
        return built.with_exec(["sh", "-ceu", copy])

    @staticmethod
    def _release_source(commit_sha: str) -> dagger.Directory:
        return dag.git(REPOSITORY_URL).commit(commit_sha).tree(depth=0, include_tags=True)

    @staticmethod
    def _distribution_command(tag: str) -> list[str]:
        return [
            "uv",
            "run",
            "python",
            "scripts/release_contract.py",
            "distributions",
            "--root",
            ".",
            "--dist",
            "dist",
            "--tag",
            tag,
        ]

    @staticmethod
    def _checksum_command() -> list[str]:
        return [
            "uv",
            "run",
            "python",
            "scripts/release_contract.py",
            "checksums",
            "--dist",
            "dist",
            "--output",
            "SHA256SUMS",
        ]

    def _hosted(self, commit: str, tag: str, token: dagger.Secret) -> dagger.Container:
        container = self._python(self.source).with_secret_variable("GITHUB_TOKEN", token)
        return container.with_exec(
            ["sh", ".dagger/scripts/github-hosted.sh", commit, tag, REPOSITORY]
        )

    def _python(self, source: dagger.Directory) -> dagger.Container:
        base = (
            self._python_toolchain()
            .with_directory("/src", source, owner="65532:65532")
            .with_workdir("/src")
        )
        base = base.with_env_variable("UV_PROJECT_ENVIRONMENT", "/opt/venv")
        base = base.with_env_variable("UV_CACHE_DIR", "/opt/uv-cache")
        base = base.with_env_variable("UV_LINK_MODE", "copy")
        base = base.with_env_variable("HOME", "/opt/home")
        base = base.with_env_variable("XDG_CACHE_HOME", "/opt/model-cache")
        base = base.with_env_variable("HF_HOME", "/opt/model-cache/huggingface")
        base = base.with_env_variable("TMPDIR", "/opt/tmp")
        base = base.with_mounted_cache(
            "/opt/uv-cache", dag.cache_volume("edge-proc-uv-nonroot"), owner="65532:65532"
        )
        base = base.with_exec(
            ["mkdir", "-p", "/opt/venv", "/opt/home", "/opt/model-cache", "/opt/tmp"]
        )
        base = base.with_exec(
            [
                "chown",
                "-R",
                "65532:65532",
                "/opt/venv",
                "/opt/home",
                "/opt/model-cache",
                "/opt/tmp",
            ]
        )
        unprivileged = base.with_user("65532:65532")
        return unprivileged.with_exec(["uv", "sync", "--frozen", "--all-extras"])

    def _quality(self, source: dagger.Directory) -> dagger.Container:
        return self._python(source).with_exec(["uv", "run", "poe", "gate"])

    @staticmethod
    def _python_toolchain() -> dagger.Container:
        uv = dag.container().from_(UV_IMAGE).file("/uv")
        return dag.container().from_(PYTHON_IMAGE).with_file("/usr/local/bin/uv", uv)

    @staticmethod
    def _actionlint() -> dagger.Container:
        return dag.container().from_(ACTIONLINT_IMAGE).with_entrypoint([]).with_workdir("/repo")

    @staticmethod
    def _gitleaks() -> dagger.Container:
        return dag.container().from_(GITLEAKS_IMAGE).with_entrypoint([])

    @staticmethod
    def _require_sha(commit: str) -> None:
        valid_length = len(commit) == SHA_LENGTH
        valid = valid_length and all(character in "0123456789abcdef" for character in commit)
        if not valid:
            raise ValueError("commit_sha must be a lowercase 40-character Git SHA")
