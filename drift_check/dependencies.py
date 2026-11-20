import subprocess
from enum import Enum
from dataclasses import dataclass
from typing import Dict, Optional
from packaging.requirements import Requirement, InvalidRequirement
from packaging.version import Version, InvalidVersion
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

class BaselineStatus(Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_INSTALLED = "NOT_INSTALLED"
    INVALID_DECLARATION = "INVALID_DECLARATION"

@dataclass
class ResolvedBaseline:
    package_name: str
    declared_specifier: str
    installed_version: Optional[str]
    status: BaselineStatus
    baseline_version: Optional[str] # The single source of truth for the analysis (only populated on MATCH)

class BaselineResolver:
    def __init__(self, target_python_exec: str):
        """Inject the Python executable discovered by EnvironmentDetector."""
        self.target_python_exec = target_python_exec

    def get_installed_version(self, package_name: str) -> Optional[str]:
        """Extracts the absolute truth from the target project's .venv."""
        script = f"import importlib.metadata; print(importlib.metadata.version('{package_name}'))"
        try:
            result = subprocess.run(
                [self.target_python_exec, "-c", script],
                capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def resolve(self, package_name: str, declared_specifier: str) -> ResolvedBaseline:
        installed_version = self.get_installed_version(package_name)
        
        if not installed_version:
            return ResolvedBaseline(
                package_name, declared_specifier, None, 
                BaselineStatus.NOT_INSTALLED, None
            )

        try:
            req = Requirement(declared_specifier)
            parsed_installed = Version(installed_version)
        except (InvalidRequirement, InvalidVersion):
            return ResolvedBaseline(
                package_name, declared_specifier, installed_version, 
                BaselineStatus.INVALID_DECLARATION, None
            )

        # req.specifier automatically handles ranges (>=, <, ~=, ==) or empty specifiers
        if parsed_installed in req.specifier:
            return ResolvedBaseline(
                package_name, declared_specifier, installed_version, 
                BaselineStatus.MATCH, installed_version
            )
        else:
            return ResolvedBaseline(
                package_name, declared_specifier, installed_version, 
                BaselineStatus.MISMATCH, None
            )

class DependencyDeclarationResolver:
    def __init__(self, target_dir: str):
        self.target_dir = Path(target_dir).resolve()

    def find(self, package_name: str) -> Optional[str]:
        """
        Finds the declared specifier for a specific package.
        Search order: requirements.txt -> pyproject.toml -> uv.lock (pinned as ==version).
        """
        target_name = canonicalize(package_name)
        return (
            self._find_in_requirements(target_name)
            or self.pyproject_dependencies().get(target_name)
            or self._find_in_uv_lock(target_name)
        )

    def declared(self) -> Dict[str, str]:
        """
        All direct dependencies, as {normalized name: specifier}.
        requirements.txt wins over pyproject.toml when a package is in both.
        """
        deps = self.pyproject_dependencies()
        deps.update(self.requirements_dependencies())
        return deps

    def _find_in_requirements(self, target_name: str) -> Optional[str]:
        req_file = self.target_dir / "requirements.txt"
        if not req_file.exists():
            return None

        with open(req_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.split(" #")[0].strip()
                # Skip comments and pip options such as -r, -e, --index-url
                if not line or line.startswith(("#", "-")):
                    continue

                # Extract the base package name from the line (dots allowed: zope.interface)
                match = re.match(r"^([a-zA-Z0-9_\-.]+)", line)
                if match and canonicalize(match.group(1)) == target_name:
                    return line  # Returns the full string, e.g., "openai>=1.50.0"
                        
        return None

    def requirements_dependencies(self) -> Dict[str, str]:
        req_file = self.target_dir / "requirements.txt"
        if not req_file.exists():
            return {}

        deps = {}
        with open(req_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                # Skip comments and pip options such as -r, -e, --index-url
                if not line or line.startswith(("#", "-")):
                    continue
                match = re.match(r"^([a-zA-Z0-9_\-.]+)", line)
                if match:
                    deps.setdefault(canonicalize(match.group(1)), line)
        return deps

    def pyproject_dependencies(self) -> Dict[str, str]:
        """
        PEP 621 [project] dependencies and optional-dependencies, plus PEP 735 [dependency-groups].
        """
        data = self._load_toml("pyproject.toml")
        if not data:
            return {}

        project = data.get("project", {})
        entries = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            entries.extend(group)
        for group in data.get("dependency-groups", {}).values():
            entries.extend(group)

        own_name = canonicalize(project.get("name", ""))
        deps = {}
        for entry in entries:
            # {include-group = "..."} tables are not requirements
            if not isinstance(entry, str):
                continue
            try:
                name = canonicalize(Requirement(entry).name)
            except InvalidRequirement:
                continue
            if name != own_name:
                deps.setdefault(name, entry)
        return deps

    def _find_in_uv_lock(self, target_name: str) -> Optional[str]:
        data = self._load_toml("uv.lock")
        if not data:
            return None
        for pkg in data.get("package", []):
            # Editable/virtual entries are the project itself, not an installed release
            if "registry" not in pkg.get("source", {}):
                continue
            if canonicalize(pkg.get("name", "")) == target_name and pkg.get("version"):
                return f"{pkg['name']}=={pkg['version']}"
        return None

    def _load_toml(self, filename: str) -> Optional[dict]:
        path = self.target_dir / filename
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError):
            return None


def canonicalize(name: str) -> str:
    """PyPI treats '-', '_' and '.' as equivalent, case-insensitively."""
    return re.sub(r"[-_.]+", "-", name).lower()
