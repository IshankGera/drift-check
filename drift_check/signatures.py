import ast
import json
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional

from drift_check.models import SignatureDefinition


class SandboxError(Exception):
    """Raised when the isolated target environment cannot be built."""


# Executed with `python -c` inside the TARGET interpreter (sandbox or project venv).
# Prints exactly one JSON object on stdout. Status values:
#   SUCCESS               -> signature extracted
#   METHOD_NOT_FOUND      -> attribute provably absent in the target version  (ERROR)
#   UNRESOLVED            -> chain passes through an instance attribute we cannot type (WARN)
#   IMPORT_FAILED         -> the package itself failed to import in the target (WARN)
#   SIGNATURE_UNAVAILABLE -> object found, but inspect.signature() is not supported (WARN)
#   UNSUPPORTED_CALLABLE  -> anything else (WARN)
INLINE_WORKER = r'''
import sys, json, importlib, inspect, typing, os, ast, textwrap
sys.path.insert(0, os.getcwd())

SDK_REGISTRY = {
    "openai.chat.completions.create": ("openai.resources.chat.completions", "Completions.create"),
}

_MISSING = object()


def emit(status, **extra):
    print(json.dumps(dict(status=status, **extra)))


def is_lazy_property(obj):
    """property, functools.cached_property and look-alikes used by SDKs."""
    if isinstance(obj, property):
        return True
    return hasattr(obj, "__set_name__") and callable(getattr(obj, "func", None))


def property_getter(obj):
    return obj.fget if isinstance(obj, property) else obj.func


def unwrap_type(tp):
    """Optional[X] / Union[X, None] -> X; returns None if not a usable class."""
    args = getattr(tp, "__args__", None)
    if args and getattr(tp, "__origin__", None) is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            tp = non_none[0]
    return tp if inspect.isclass(tp) else None


def resolve_annotation(ann, func):
    if isinstance(ann, str):
        try:
            ann = eval(ann, getattr(func, "__globals__", {}))
        except Exception:
            return None
    return unwrap_type(ann)


def return_type(func):
    try:
        hints = typing.get_type_hints(func)
        if "return" in hints:
            return unwrap_type(hints["return"])
    except Exception:
        pass
    ann = getattr(func, "__annotations__", {}).get("return")
    return resolve_annotation(ann, func) if ann is not None else None


def instance_attr_type(cls, name):
    """
    Look for `name` as an instance attribute of cls (searching the MRO).
    Returns (found, type_or_None). Uses, in order:
      1. class-level annotations  (chat: Chat / "resources.Chat")
      2. `self.name: T = ...` annotations inside methods
      3. `self.name = SomeClass(...)` constructor calls inside methods
    """
    found_anywhere = False
    for klass in inspect.getmro(cls):
        if klass is object:
            continue
        module_globals = vars(sys.modules.get(klass.__module__, sys))

        raw = klass.__dict__.get("__annotations__", {})
        if name in raw:
            found_anywhere = True
            ann = raw[name]
            if isinstance(ann, str):
                try:
                    ann = eval(ann, module_globals)
                except Exception:
                    ann = None
            typ = unwrap_type(ann) if ann is not None else None
            if typ is not None:
                return True, typ

        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(klass)))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for t in targets:
                if not (isinstance(t, ast.Attribute) and t.attr == name
                        and isinstance(t.value, ast.Name) and t.value.id == "self"):
                    continue
                found_anywhere = True
                candidates = []
                if isinstance(node, ast.AnnAssign):
                    candidates.append(node.annotation)
                if isinstance(node.value, ast.Call):
                    candidates.append(node.value.func)
                for expr in candidates:
                    try:
                        typ = unwrap_type(eval(ast.unparse(expr), module_globals))
                    except Exception:
                        typ = None
                    if typ is not None:
                        return True, typ
    return found_anywhere, None


def traverse(package_name, method_name):
    """
    Walk package.method_name. Returns (status, obj).
    Attributes reached through an instance (property / self.x) are followed via
    their type hints; when no hint exists the result is UNRESOLVED, never an ERROR.
    """
    full_call = f"{package_name}.{method_name}"
    try:
        if full_call in SDK_REGISTRY:
            mod_path, class_path = SDK_REGISTRY[full_call]
            obj = importlib.import_module(mod_path)
            parts = class_path.split(".")
        else:
            obj = importlib.import_module(package_name)
            parts = method_name.split(".")
    except Exception as e:
        return "IMPORT_FAILED", f"{type(e).__name__}: {e}"

    for i, part in enumerate(parts):
        if obj.__class__.__name__ == "APIRemovedInV1Proxy":
            return "METHOD_NOT_FOUND", None
        try:
            if inspect.isclass(obj):
                # Look up without triggering descriptors so properties are visible.
                nxt = inspect.getattr_static(obj, part, _MISSING)
            else:
                nxt = getattr(obj, part, _MISSING)
        except Exception:
            # Custom SDK removal proxies raise on attribute access
            return "METHOD_NOT_FOUND", None

        if nxt is _MISSING:
            if inspect.isclass(obj):
                found, typ = instance_attr_type(obj, part)
                if found:
                    if typ is None:
                        return "UNRESOLVED", None
                    obj = typ
                    continue
            return "METHOD_NOT_FOUND", None

        if inspect.isclass(obj) and is_lazy_property(nxt):
            typ = return_type(property_getter(nxt))
            if typ is None:
                return "UNRESOLVED", None
            obj = typ
            continue

        if inspect.isclass(obj) and isinstance(nxt, (staticmethod, classmethod)):
            nxt = getattr(obj, part)

        obj = nxt

    if obj.__class__.__name__ == "APIRemovedInV1Proxy":
        return "METHOD_NOT_FOUND", None
    return "SUCCESS", obj


def main():
    package_name, method_name = sys.argv[1], sys.argv[2]
    status, obj = traverse(package_name, method_name)
    if status == "IMPORT_FAILED":
        emit(status, detail=obj)
        return
    if status != "SUCCESS":
        emit(status)
        return

    try:
        sig = inspect.signature(obj)
    except ValueError:
        emit("SIGNATURE_UNAVAILABLE")
        return
    except Exception:
        emit("UNSUPPORTED_CALLABLE")
        return

    params = [n for n in sig.parameters if n not in ("self", "cls")]
    has_varkw = False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            has_varkw = True
            ann = param.annotation
            if ann is not inspect.Parameter.empty:
                type_args = getattr(ann, "__args__", None)
                target_type = type_args[0] if type_args else ann
                if hasattr(target_type, "__annotations__"):
                    params.extend(target_type.__annotations__.keys())
                    has_varkw = False

    emit("SUCCESS", valid_parameters=sorted(set(params)), accepts_kwargs=has_varkw)


if __name__ == "__main__":
    main()
'''

# Statuses for which a static stub lookup cannot improve the answer.
_NO_STUB_FALLBACK = {"METHOD_NOT_FOUND", "IMPORT_FAILED", "UNRESOLVED"}


class RuntimeProvider:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def resolve(self, python_exec: str, package_name: str, method_name: str) -> dict:
        result = subprocess.run(
            [python_exec, "-c", INLINE_WORKER, package_name, method_name],
            capture_output=True,
            text=True,
        )
        lines = result.stdout.strip().splitlines()
        if result.returncode != 0 or not lines:
            if self.verbose:
                print("\n--- RUNTIME WORKER ERROR ---")
                print("Python:", python_exec, "| Call:", f"{package_name}.{method_name}")
                print("STDERR:", result.stderr)
                print("----------------------------\n")
            return {"status": "UNSUPPORTED_CALLABLE"}
        try:
            # Imported packages may print on import; the worker's JSON is the last line.
            return json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"status": "UNSUPPORTED_CALLABLE"}


class StubProvider:
    """Fallback: parses .pyi stubs (or typed .py files) of the package in the TARGET interpreter."""

    _FIND_ORIGIN = (
        "import importlib.util, sys\n"
        "s = importlib.util.find_spec(sys.argv[1])\n"
        "print(s.origin if s and s.origin else '')"
    )

    def resolve(self, python_exec: str, package_name: str, method_name: str) -> Optional[List[str]]:
        try:
            out = subprocess.run(
                [python_exec, "-c", self._FIND_ORIGIN, package_name],
                capture_output=True, text=True,
            )
            base_path = out.stdout.strip()
            if not base_path.endswith(".py"):
                return None
            pyi_path = base_path + "i"
            target_path = pyi_path if os.path.exists(pyi_path) else base_path
            with open(target_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            return None

        parts = method_name.split(".")
        scope = tree
        # Walk class names (e.g. Client.create) so we don't match an unrelated function.
        for cls_name in parts[:-1]:
            scope = next(
                (n for n in getattr(scope, "body", []) if isinstance(n, ast.ClassDef) and n.name == cls_name),
                None,
            )
            if scope is None:
                return None
        for node in getattr(scope, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[-1]:
                args = [a.arg for a in node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
                return args
        return None


def find_uv() -> Optional[str]:
    """Prefer the uv binary shipped as a Python dependency, then fall back to PATH."""
    try:
        from uv import find_uv_bin
        return find_uv_bin()
    except Exception:
        return shutil.which("uv")


class SignatureResolver:
    def __init__(
        self,
        project_python_exec: str,
        target_version: Optional[str] = None,
        package_to_sandbox: Optional[str] = None,
        local_wheel: Optional[str] = None,
        verbose: bool = False,
    ):
        self.project_python_exec = project_python_exec
        self.target_version = target_version
        self.package_to_sandbox = package_to_sandbox
        self.verbose = verbose
        self.local_wheel = os.path.abspath(local_wheel) if local_wheel else None

        self.sandbox_dir = None
        self.sandbox_python_exec = None

        self.runtime_provider = RuntimeProvider(verbose=verbose)
        self.stub_provider = StubProvider()

        if self.target_version and self.package_to_sandbox:
            self._setup_sandbox()

    def _run_uv(self, uv: str, args: List[str], cwd: str, step: str):
        try:
            subprocess.run([uv] + args, cwd=cwd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip()
            raise SandboxError(f"{step} failed.\n{detail}") from None

    def _setup_sandbox(self):
        uv = find_uv()
        if not uv:
            raise SandboxError(
                "'uv' was not found. Install it with `pip install uv` "
                "(see https://docs.astral.sh/uv/) and try again."
            )

        self.sandbox_dir = tempfile.TemporaryDirectory(prefix="drift-check-")
        path = self.sandbox_dir.name

        # Match the project's interpreter so the sandbox resolves the same wheels.
        self._run_uv(uv, ["venv", "--python", self.project_python_exec], path, "Creating the sandbox")

        if os.name == "nt":
            self.sandbox_python_exec = os.path.join(path, ".venv", "Scripts", "python.exe")
        else:
            self.sandbox_python_exec = os.path.join(path, ".venv", "bin", "python")

        install_target = self.local_wheel or f"{self.package_to_sandbox}=={self.target_version}"
        self._run_uv(
            uv,
            ["pip", "install", "--python", self.sandbox_python_exec, install_target],
            path,
            f"Installing {install_target} into the sandbox",
        )

    def resolve(self, package_name: str, method_name: str) -> SignatureDefinition:
        target_exec = (
            self.sandbox_python_exec
            if (
                self.sandbox_dir
                and package_name.replace("-", "_").lower()
                == self.package_to_sandbox.replace("-", "_").lower()
            )
            else self.project_python_exec
        )

        data = self.runtime_provider.resolve(target_exec, package_name, method_name)
        if self.verbose:
            print(f"[verbose] {package_name}.{method_name} via {target_exec}: {data}")
        status = data.get("status", "UNSUPPORTED_CALLABLE")

        if status == "SUCCESS":
            return SignatureDefinition(
                package_name=package_name,
                method_name=method_name,
                valid_parameters=data.get("valid_parameters", []),
                is_unknown=False,
                accepts_kwargs=data.get("accepts_kwargs", False),
                error_code="SUCCESS",
            )

        # Static stubs can only help when the object exists but couldn't be inspected.
        # Never use them to overrule a definite METHOD_NOT_FOUND from the target sandbox.
        if status not in _NO_STUB_FALLBACK:
            params = self.stub_provider.resolve(target_exec, package_name, method_name)
            if params is not None:
                return SignatureDefinition(package_name, method_name, params, is_unknown=False, error_code="SUCCESS")

        return SignatureDefinition(package_name, method_name, [], is_unknown=True, error_code=status)
