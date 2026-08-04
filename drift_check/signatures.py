import importlib
import importlib.util
import inspect
import subprocess
import tempfile
import json
import os
import ast
import typing
from typing import List, Optional
from drift_check.models import SignatureDefinition

# The inline worker now handles both standard signatures and TypedDicts securely in the target environment
INLINE_WORKER = """
import sys, json, importlib, inspect, typing
sys.path.insert(0, os.getcwd())

SDK_REGISTRY = {
    "openai.chat.completions.create": ("openai.resources.chat.completions", "Completions.create")
}

def main():
    package_name, method_name = sys.argv[1], sys.argv[2]
    full_call = f"{package_name}.{method_name}"
    
    try:
        # 1. Resolve physical path via Registry or standard traversal
        if full_call in SDK_REGISTRY:
            mod_path, class_path = SDK_REGISTRY[full_call]
            current_obj = importlib.import_module(mod_path)
            for part in class_path.split('.'):
                current_obj = getattr(current_obj, part)
        else:
            current_obj = importlib.import_module(package_name)
            for part in method_name.split('.'):
                current_obj = getattr(current_obj, part)
                
        # 2. Standard Runtime Inspection
        sig = inspect.signature(current_obj)
        params = list(sig.parameters.keys())
        has_varkw = False
        
        # 3. Targeted TypedDict Extraction
        for param in sig.parameters.values():
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                has_varkw = True
                
                # Check if kwargs is annotated with Unpack[TypedDict]
                ann = param.annotation
                if ann != inspect.Parameter.empty:
                    type_args = getattr(ann, '__args__', None)
                    target_type = type_args[0] if type_args else ann
                    
                    if hasattr(target_type, '__annotations__'):
                        # Found the TypedDict! Append its keys to our valid parameters
                        params.extend(target_type.__annotations__.keys())
                        # Since we successfully unpacked it, it is no longer an unknown wildcard
                        has_varkw = False 
        
        print(json.dumps({
            "status": "SUCCESS",
            "valid_parameters": list(set(params)),
            "accepts_kwargs": has_varkw
        }))
        
    except ModuleNotFoundError:
        print(json.dumps({"status": "PACKAGE_NOT_INSTALLED"}))
    except AttributeError:
        print(json.dumps({"status": "METHOD_NOT_FOUND"}))
    except ValueError:
        print(json.dumps({"status": "SIGNATURE_UNAVAILABLE"}))
    except Exception:
        print(json.dumps({"status": "UNSUPPORTED_CALLABLE"}))

if __name__ == '__main__':
    main()
"""

class RuntimeProvider:
    """Executes signature inspection securely within the target environment."""
    def resolve(self, python_exec: str, package_name: str, method_name: str) -> dict:
        result = subprocess.run(
            [python_exec, "-c", INLINE_WORKER, package_name, method_name],
            capture_output=True, text=True
        )
        try:
            return json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return {"status": "UNSUPPORTED_CALLABLE"}

class StubProvider:
    """V2: Parses .pyi files (or typed .py files) using AST to extract signatures."""
    def resolve(self, package_name: str, method_name: str) -> Optional[List[str]]:
        try:
            spec = importlib.util.find_spec(package_name)
            if not spec or not spec.origin:
                return None
                
            base_path = spec.origin
            
            if base_path.endswith('.py'):
                pyi_path = base_path + 'i'
                target_path = pyi_path if os.path.exists(pyi_path) else base_path
            else:
                return None 
                
            if not os.path.exists(target_path):
                return None

            with open(target_path, 'r', encoding='utf-8') as f:
                tree = ast.parse(f.read())
                
            target_func = method_name.split('.')[-1]
            
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == target_func:
                        args = [arg.arg for arg in node.args.args if arg.arg != 'self']
                        args.extend([arg.arg for arg in node.args.kwonlyargs])
                        return args
                        
        except Exception:
            pass
        return None

class SignatureResolver:
    def __init__(self, project_python_exec: str, target_version: Optional[str] = None, package_to_sandbox: Optional[str] = None):
        self.project_python_exec = project_python_exec
        self.target_version = target_version
        self.package_to_sandbox = package_to_sandbox
        self.sandbox_dir = None
        self.sandbox_python_exec = None
        
        self.runtime_provider = RuntimeProvider()
        self.stub_provider = StubProvider()
        
        if self.target_version and self.package_to_sandbox:
            self._setup_sandbox()

    def _setup_sandbox(self):
        self.sandbox_dir = tempfile.TemporaryDirectory()
        path = self.sandbox_dir.name
        
        # 1. Create the virtual environment
        subprocess.run(["uv", "venv"], cwd=path, check=True, capture_output=True)
        
        self.sandbox_python_exec = os.path.join(path, ".venv", "Scripts", "python.exe") if os.name == 'nt' else os.path.join(path, ".venv", "bin", "python")
            
        # 2. Install the package explicitly using the sandbox's python executable
        # This strictly binds the installation to the sandbox, ignoring host environments
        subprocess.run(
            ["uv", "pip", "install", "--python", self.sandbox_python_exec, f"{self.package_to_sandbox}=={self.target_version}"],
            cwd=path, check=True, capture_output=True
        )

    def resolve(self, package_name: str, method_name: str) -> SignatureDefinition:
        target_exec = self.sandbox_python_exec if (self.sandbox_dir and package_name == self.package_to_sandbox) else self.project_python_exec
        
        # 1. Run dynamic worker in the target environment
        data = self.runtime_provider.resolve(target_exec, package_name, method_name)
        status = data.get("status", "UNSUPPORTED_CALLABLE")
        
        if status == "SUCCESS":
            return SignatureDefinition(
                package_name=package_name,
                method_name=method_name,
                valid_parameters=data.get("valid_parameters", []),
                is_unknown=False,
                accepts_kwargs=data.get("accepts_kwargs", False),
                error_code="SUCCESS"
            )
            
        # 2. Fallback to static .pyi stubs
        params = self.stub_provider.resolve(package_name, method_name)
        if params is not None:
            return SignatureDefinition(package_name, method_name, params, is_unknown=False, error_code="SUCCESS")

        # 3. Unresolvable error
        return SignatureDefinition(package_name, method_name, [], is_unknown=True, error_code=status)