import subprocess
from enum import Enum
from dataclasses import dataclass
from typing import Optional
from packaging.requirements import Requirement, InvalidRequirement
from packaging.version import Version, InvalidVersion
import re
from pathlib import Path

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
        Parses requirements.txt to find the declared specifier for a specific package.
        MVP: Only supports requirements.txt.
        """
        req_file = self.target_dir / "requirements.txt"
        if not req_file.exists():
            return None

        # Normalize package name (PyPI treats '-' and '_' as equivalent)
        target_name = package_name.lower().replace("_", "-")
        
        with open(req_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                # Extract the base package name from the line
                match = re.match(r"^([a-zA-Z0-9_\-]+)(.*)$", line)
                if match:
                    name_in_file = match.group(1).strip().lower().replace("_", "-")
                    if name_in_file == target_name:
                        return line # Returns the full string, e.g., "openai>=1.50.0"
                        
        return None