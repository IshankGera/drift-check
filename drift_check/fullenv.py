"""
A disposable copy of the project's whole environment, with one package moved to a new version.

`upgrade` only needs the target package in its sandbox. `typecheck` and `test` need everything
the project imports, so this sandbox is built from the project's installed packages
(`uv pip freeze`), and then the target package is installed on top.
"""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from packaging.utils import canonicalize_name

from drift_check.signatures import SandboxError, find_uv


class ProjectSandbox:
    def __init__(
        self,
        project_python: str,
        package: str,
        target_version: str,
        local_wheel: Optional[str] = None,
        extra_packages: Optional[List[str]] = None,
    ):
        self.project_python = project_python
        self.package = package
        self.target_version = target_version
        self.local_wheel = os.path.abspath(local_wheel) if local_wheel else None
        self.extra_packages = extra_packages or []
        self._tmp = tempfile.TemporaryDirectory(prefix="drift-check-full-")
        self.dir = Path(self._tmp.name)
        venv = self.dir / ".venv"
        self.python = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        self.uv = find_uv()
        if not self.uv:
            raise SandboxError("'uv' was not found. Install it with `pip install uv` and try again.")

    def _uv(self, args: List[str], step: str) -> str:
        try:
            out = subprocess.run([self.uv] + args, cwd=self.dir, check=True, capture_output=True, text=True)
            return out.stdout
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip()
            raise SandboxError(f"{step} failed.\n{detail}") from None

    def build(self) -> "ProjectSandbox":
        self._uv(["venv", "--python", self.project_python], "Creating the sandbox")

        frozen = self._uv(
            ["pip", "freeze", "--python", self.project_python, "--exclude-editable"],
            "Reading the project's installed packages",
        )
        keep = [line for line in frozen.splitlines() if self._keep(line)]
        if keep:
            requirements = self.dir / "requirements.frozen.txt"
            requirements.write_text("\n".join(keep) + "\n", encoding="utf-8")
            self._uv(["pip", "install", "--python", self.python, "-r", str(requirements)],
                     "Installing the project's current packages")

        target = self.local_wheel or f"{self.package}=={self.target_version}"
        self._uv(["pip", "install", "--python", self.python, target, *self.extra_packages],
                 f"Installing {target}")
        return self

    def _keep(self, line: str) -> bool:
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            return False
        name = line.split("==")[0].split(" @ ")[0].split("[")[0].strip()
        # The target package is installed separately at its new version
        return canonicalize_name(name) != canonicalize_name(self.package)

    def cleanup(self):
        try:
            self._tmp.cleanup()
        except Exception:
            pass  # Windows can hold locks briefly; the OS cleans temp folders eventually


def project_pythonpath(root: Path) -> str:
    """Make the project importable without installing it: the root, plus src/ when present."""
    paths = [str(root)]
    if (root / "src").is_dir():
        paths.append(str(root / "src"))
    existing = os.environ.get("PYTHONPATH")
    if existing:
        paths.append(existing)
    return os.pathsep.join(paths)
