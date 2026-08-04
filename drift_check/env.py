import os
import sys
from pathlib import Path

class EnvironmentDetector:
    def __init__(self, target_dir: str):
        self.target_dir = Path(target_dir).resolve()

    def find_python_executable(self) -> str:
        """
        Finds the most appropriate Python interpreter for the target project.
        Search order: VIRTUAL_ENV variable -> .venv/ -> venv/ -> env/ -> sys.executable
        """
        
        # 1. Check for an actively sourced virtual environment
        active_venv = os.environ.get("VIRTUAL_ENV")
        if active_venv:
            if os.name == 'nt':
                exec_path = Path(active_venv) / "Scripts" / "python.exe"
            else:
                exec_path = Path(active_venv) / "bin" / "python"
                
            if exec_path.exists():
                return str(exec_path)

        # 2. Check for standard local environment folders
        venv_names = [".venv", "venv", "env"]
        for venv_name in venv_names:
            venv_path = self.target_dir / venv_name
            if venv_path.is_dir():
                if os.name == 'nt':
                    exec_path = venv_path / "Scripts" / "python.exe"
                else:
                    exec_path = venv_path / "bin" / "python"
                    
                if exec_path.exists():
                    return str(exec_path)
        
        # 3. Fallback to the active interpreter running the CLI
        return sys.executable