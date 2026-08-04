import os
from pathlib import Path
from typing import Tuple, List

class ProjectScanner:
    def __init__(self, target_dir: str):
        self.target_dir = Path(target_dir).resolve()
        self.ignore_dirs = {
            ".git", ".venv", "venv", "__pycache__", 
            "build", "dist", "node_modules"
        }
        self.dependency_filenames = {"requirements.txt", "pyproject.toml", "uv.lock"}

    def scan(self) -> Tuple[List[Path], List[Path]]:
        """
        Walks the directory and returns a tuple of (python_files, dependency_files).
        """
        python_files = []
        dependency_files = []

        if not self.target_dir.exists() or not self.target_dir.is_dir():
            return python_files, dependency_files

        for root, dirs, files in os.walk(self.target_dir):
            # TODO: Parse .gitignore here to dynamically add to ignore_dirs
            # Mutate the dirs list in-place to skip ignored directories
            dirs[:] = [d for d in dirs if d not in self.ignore_dirs]

            root_path = Path(root)

            for file in files:
                if file.endswith(".py"):
                    python_files.append(root_path / file)
                elif file in self.dependency_filenames:
                    dependency_files.append(root_path / file)

        return python_files, dependency_files