from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RevisionResult:
    valid: bool
    message: str
    revision: str = ""


class GitRevisionMonitor:
    """Validate a revision already pulled into the local working tree."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir.resolve()

    def _git(
        self,
        *arguments: str,
        timeout: int = 30,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.project_dir}",
                *arguments,
            ],
            cwd=self.project_dir,
            capture_output=True,
            text=text,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def _detail(completed: subprocess.CompletedProcess) -> str:
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        return (stderr or stdout or f"exit code {completed.returncode}").strip()[-1000:]

    def current_revision(self) -> str:
        if not (self.project_dir / ".git").exists():
            return ""
        try:
            completed = self._git("rev-parse", "HEAD", timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def validate_revision(
        self, old_revision: str, new_revision: str
    ) -> RevisionResult:
        if not old_revision or not new_revision or old_revision == new_revision:
            return RevisionResult(True, "代码版本没有变化", new_revision)
        try:
            whitespace = self._git(
                "diff", "--check", old_revision, new_revision, timeout=30
            )
            if whitespace.returncode != 0:
                return RevisionResult(
                    False,
                    f"代码格式检查失败：{self._detail(whitespace)}",
                    new_revision,
                )
            changed = self._git(
                "diff",
                "--name-only",
                "--diff-filter=ACMR",
                old_revision,
                new_revision,
                "--",
                "*.py",
                timeout=30,
            )
            if changed.returncode != 0:
                return RevisionResult(
                    False,
                    f"无法检查更新内容：{self._detail(changed)}",
                    new_revision,
                )
            compiled = 0
            for relative in filter(None, changed.stdout.splitlines()):
                source = self._git("show", f"{new_revision}:{relative}", text=False)
                if source.returncode != 0:
                    return RevisionResult(False, f"无法读取更新文件：{relative}")
                compile(source.stdout, relative, "exec")
                compiled += 1
            requirements = self._git(
                "diff",
                "--name-only",
                old_revision,
                new_revision,
                "--",
                "requirements.txt",
                timeout=30,
            )
            dependency_changed = bool(
                requirements.returncode == 0 and requirements.stdout.strip()
            )
            message = f"已检查 {compiled} 个 Python 文件"
            if dependency_changed:
                message += "；依赖清单有变化，请运行安装脚本后再重启服务"
            return RevisionResult(
                not dependency_changed,
                message,
                new_revision,
            )
        except (OSError, SyntaxError, subprocess.SubprocessError) as exc:
            return RevisionResult(False, str(exc), new_revision)

