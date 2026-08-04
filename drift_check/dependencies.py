import re
from pathlib import Path
from typing import List
from drift_check.models import Dependency

class DependencyResolver:
    def __init__(self, dependency_files: List[Path]):
        self.dependency_files = dependency_files

    def resolve(self) -> List[Dependency]:
        dependencies = []
        
        for file_path in self.dependency_files:
            if file_path.name == "requirements.txt":
                dependencies.extend(self._parse_requirements_txt(file_path))
                
        return dependencies

    def _parse_requirements_txt(self, file_path: Path) -> List[Dependency]:
        deps = []
        pattern = re.compile(r"^([a-zA-Z0-9_\-]+)(.*)$")
        
        with open(file_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                    
                match = pattern.match(line)
                if match:
                    name = match.group(1).strip()
                    specifier = match.group(2).strip()
                    is_pinned = "==" in specifier
                    
                    deps.append(Dependency(
                        name=name,
                        installed_version=None,
                        is_pinned=is_pinned,
                        specifier=specifier
                    ))
        return deps