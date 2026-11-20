import ast
import json
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

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


def lazy_import_namespace(func):
    """
    Names imported lazily or only for type checking, e.g. openai 2.x:
        @cached_property
        def chat(self) -> Chat:
            from .resources.chat import Chat
    Collects `from X import Name` statements inside the function and at the top of its
    module (including under `if TYPE_CHECKING:`), and imports those names on demand.
    """
    namespace = {}
    module_name = getattr(func, "__module__", "") or ""
    module = sys.modules.get(module_name)
    package = getattr(module, "__package__", None) or module_name.rpartition(".")[0]
    sources = []
    try:
        sources.append(textwrap.dedent(inspect.getsource(func)))
    except Exception:
        pass
    try:
        sources.append(inspect.getsource(module))
    except Exception:
        pass
    for src in sources:
        try:
            tree = ast.parse(src)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module and not node.level:
                continue
            target = ("." * node.level) + (node.module or "")
            for alias in node.names:
                local = alias.asname or alias.name
                if local in namespace:
                    continue
                try:
                    mod = importlib.import_module(target, package=package if node.level else None)
                    namespace[local] = getattr(mod, alias.name)
                except Exception:
                    try:
                        sub = importlib.import_module(target + "." + alias.name if node.module else target + alias.name,
                                                      package=package if node.level else None)
                        namespace[local] = sub
                    except Exception:
                        pass
    return namespace


def eval_in(expr, namespace, owner):
    """eval() an annotation/expression string, falling back to lazily imported names."""
    try:
        return eval(expr, namespace)
    except Exception:
        return eval(expr, {**namespace, **lazy_import_namespace(owner)})


def resolve_annotation(ann, func):
    if isinstance(ann, str):
        func_globals = getattr(func, "__globals__", {})
        try:
            ann = eval(ann, func_globals)
        except Exception:
            try:
                ann = eval(ann, {**func_globals, **lazy_import_namespace(func)})
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
                    ann = eval_in(ann, module_globals, klass)
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
                        typ = unwrap_type(eval_in(ast.unparse(expr), module_globals, klass))
                    except Exception:
                        typ = None
                    if typ is not None:
                        return True, typ
    return found_anywhere, None


def _is_api_member(value):
    """Things on a class that are part of its API (as opposed to data defaults like `x = None`)."""
    return (
        inspect.isroutine(value)
        or inspect.isclass(value)
        or isinstance(value, (staticmethod, classmethod))
        or is_lazy_property(value)
        or inspect.ismodule(value)
    )


def traverse(package_name, method_name):
    """
    Walk package.method_name. Returns (status, obj, drop_first).
    drop_first is True when the final object is a plain function reached through a class,
    so its first parameter is the implicit `self` and must not be counted.
    Attributes reached through an instance (property / self.x / annotated field) are followed
    via their type hints; when no hint exists the result is UNRESOLVED, never an ERROR.
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
        return "IMPORT_FAILED", f"{type(e).__name__}: {e}", False

    drop_first = False
    for part in parts:
        drop_first = False
        if obj.__class__.__name__ == "APIRemovedInV1Proxy":
            return "METHOD_NOT_FOUND", None, False
        try:
            if inspect.isclass(obj):
                # Look up without triggering descriptors so properties are visible.
                nxt = inspect.getattr_static(obj, part, _MISSING)
            else:
                nxt = getattr(obj, part, _MISSING)
        except Exception:
            # Custom SDK removal proxies raise on attribute access
            return "METHOD_NOT_FOUND", None, False

        if nxt is _MISSING and inspect.ismodule(obj):
            # Submodules are only attributes once imported: pkg.sub.fn
            submodule = f"{obj.__name__}.{part}"
            try:
                nxt = importlib.import_module(submodule)
            except ModuleNotFoundError as e:
                if e.name != submodule:
                    return "IMPORT_FAILED", f"{type(e).__name__}: {e}", False
                nxt = _MISSING
            except Exception as e:
                return "IMPORT_FAILED", f"{type(e).__name__}: {e}", False

        if inspect.isclass(obj) and (nxt is _MISSING or not _is_api_member(nxt)):
            # Instance data: `session: Session = None`, dataclass/pydantic fields, __slots__,
            # or attributes only set in __init__. Follow the declared type instead of the default.
            found, typ = instance_attr_type(obj, part)
            if typ is not None:
                obj = typ
                continue
            if found or nxt is not _MISSING:
                return "UNRESOLVED", None, False
            return "METHOD_NOT_FOUND", None, False

        if nxt is _MISSING:
            return "METHOD_NOT_FOUND", None, False

        if inspect.isclass(obj) and is_lazy_property(nxt):
            typ = return_type(property_getter(nxt))
            if typ is None:
                return "UNRESOLVED", None, False
            obj = typ
            continue

        if inspect.isclass(obj) and isinstance(nxt, (staticmethod, classmethod)):
            nxt = getattr(obj, part)  # bound/unwrapped: no implicit first argument
        elif inspect.isclass(obj) and inspect.isfunction(nxt):
            drop_first = True          # plain method looked up on the class: first param is self

        obj = nxt

    if obj.__class__.__name__ == "APIRemovedInV1Proxy":
        return "METHOD_NOT_FOUND", None, False
    return "SUCCESS", obj, drop_first


_DEPRECATION_HINTS = ("Deprecat", "FutureWarning")


def deprecation_of(obj):
    """
    Message if obj is marked deprecated, else None. Detects:
      - PEP 702 @deprecated / typing_extensions.deprecated (sets __deprecated__)
      - warnings.warn(..., <Deprecation|Future>Warning) as a top-level statement of the
        function body (warnings inside if/for blocks are conditional, so they are ignored)
    """
    msg = getattr(obj, "__deprecated__", None)
    if isinstance(msg, str) and msg:
        return msg
    func = obj
    if inspect.isclass(obj):
        func = obj.__dict__.get("__init__")
    func = getattr(func, "__func__", func)
    if not callable(func):
        return None
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except Exception:
        return None
    fn_node = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if fn_node is None:
        return None
    for stmt in fn_node.body:
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if not isinstance(call, ast.Call):
            continue
        name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
        if name != "warn":
            continue
        category = call.args[1] if len(call.args) > 1 else next(
            (k.value for k in call.keywords if k.arg == "category"), None)
        category_name = ""
        if isinstance(category, ast.Name):
            category_name = category.id
        elif isinstance(category, ast.Attribute):
            category_name = category.attr
        if any(h in category_name for h in _DEPRECATION_HINTS):
            text = call.args[0] if call.args else None
            if isinstance(text, ast.Constant) and isinstance(text.value, str):
                return text.value
            return f"{getattr(obj, '__qualname__', 'This API')} is deprecated."
    return None


def unpacked_typeddict(annotation):
    """For `**kwargs: Unpack[SomeTypedDict]`, return the TypedDict class, else None."""
    origin = getattr(annotation, "__origin__", None)
    unpack_forms = [getattr(typing, "Unpack", None)]
    try:
        import typing_extensions
        unpack_forms.append(getattr(typing_extensions, "Unpack", None))
    except ImportError:
        pass
    if origin is None or not any(origin is u for u in unpack_forms if u is not None):
        return None
    args = getattr(annotation, "__args__", None) or ()
    if len(args) != 1:
        return None
    inner = args[0]
    if isinstance(inner, type) and issubclass(inner, dict) and hasattr(inner, "__total__"):
        return inner  # TypedDict from typing or typing_extensions
    return None


def model_field_names(obj):
    """Field names of pydantic models, which may only expose aliases in their signature."""
    if not inspect.isclass(obj):
        return []
    fields = getattr(obj, "model_fields", None)  # pydantic v2
    if not isinstance(fields, dict):
        fields = getattr(obj, "__fields__", None)  # pydantic v1
    return list(fields) if isinstance(fields, dict) else []


def main():
    package_name, method_name = sys.argv[1], sys.argv[2]
    status, obj, drop_first = traverse(package_name, method_name)
    if status == "IMPORT_FAILED":
        emit(status, detail=obj)
        return
    if status != "SUCCESS":
        emit(status)
        return

    try:
        deprecated = deprecation_of(obj)
    except Exception:
        deprecated = None

    try:
        sig = inspect.signature(obj)
    except ValueError:
        emit("SIGNATURE_UNAVAILABLE", deprecated=deprecated)
        return
    except Exception:
        emit("UNSUPPORTED_CALLABLE", deprecated=deprecated)
        return

    P = inspect.Parameter
    items = list(sig.parameters.items())
    if drop_first and items and items[0][1].kind in (P.POSITIONAL_ONLY, P.POSITIONAL_OR_KEYWORD):
        items = items[1:]  # implicit `self` of a method looked up on its class

    params, positional_names, required = [], [], []
    has_varkw = has_varargs = False
    for name, param in items:
        if param.kind == P.VAR_POSITIONAL:
            has_varargs = True
        elif param.kind == P.VAR_KEYWORD:
            typed = unpacked_typeddict(param.annotation) if param.annotation is not P.empty else None
            if typed is not None:
                # **kwargs: Unpack[Options] -> exactly the TypedDict's keys
                params.extend(getattr(typed, "__annotations__", {}).keys())
            else:
                has_varkw = True
        else:
            if param.kind != P.POSITIONAL_ONLY:
                params.append(name)
            if param.kind in (P.POSITIONAL_ONLY, P.POSITIONAL_OR_KEYWORD):
                positional_names.append(name)
            if param.default is P.empty:
                required.append(name)

    params.extend(model_field_names(obj))

    emit(
        "SUCCESS",
        valid_parameters=sorted(set(params)),
        accepts_kwargs=has_varkw,
        positional_names=positional_names,
        max_positional=None if has_varargs else len(positional_names),
        required_params=[r for r in required if r in positional_names or r in params],
        deprecated=deprecated,
    )


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

    def resolve(self, python_exec: str, package_name: str, method_name: str) -> Optional[Tuple[List[str], bool]]:
        """(parameter names, accepts **kwargs) from the package's .pyi/.py source, or None."""
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
        # All definitions with this name: @overload variants plus the implementation
        params: List[str] = []
        accepts_kwargs = False
        found = False
        in_class = scope is not tree
        for node in getattr(scope, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[-1]:
                found = True
                positional = getattr(node.args, "posonlyargs", []) + node.args.args
                is_static = any(getattr(d, "id", None) == "staticmethod" for d in node.decorator_list)
                if in_class and not is_static and positional:
                    positional = positional[1:]  # implicit self / cls
                params.extend(a.arg for a in node.args.args + node.args.kwonlyargs if a in positional
                              or a in node.args.kwonlyargs)
                accepts_kwargs = accepts_kwargs or node.args.kwarg is not None
        if not found:
            return None
        return sorted(set(params)), accepts_kwargs


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
        # One worker run per unique (package, method): large codebases repeat the same calls a lot.
        self._cache = {}

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
        """Signature in the TARGET version (the sandbox)."""
        key = ("target", package_name, method_name)
        if key not in self._cache:
            target_exec = (
                self.sandbox_python_exec
                if (
                    self.sandbox_dir
                    and package_name.replace("-", "_").lower()
                    == self.package_to_sandbox.replace("-", "_").lower()
                )
                else self.project_python_exec
            )
            self._cache[key] = self._resolve_with(target_exec, package_name, method_name)
        return self._cache[key]

    def resolve_baseline(self, package_name: str, method_name: str) -> SignatureDefinition:
        """Signature in the CURRENTLY INSTALLED version (the project's environment)."""
        key = ("baseline", package_name, method_name)
        if key not in self._cache:
            self._cache[key] = self._resolve_with(self.project_python_exec, package_name, method_name)
        return self._cache[key]

    def _resolve_with(self, python_exec: str, package_name: str, method_name: str) -> SignatureDefinition:
        data = self.runtime_provider.resolve(python_exec, package_name, method_name)
        if self.verbose:
            print(f"[verbose] {package_name}.{method_name} via {python_exec}: {data}")
        status = data.get("status", "UNSUPPORTED_CALLABLE")

        if status == "SUCCESS":
            return SignatureDefinition(
                package_name=package_name,
                method_name=method_name,
                valid_parameters=data.get("valid_parameters", []),
                is_unknown=False,
                accepts_kwargs=data.get("accepts_kwargs", False),
                error_code="SUCCESS",
                positional_names=data.get("positional_names", []),
                max_positional=data.get("max_positional"),
                required_params=data.get("required_params", []),
                deprecated=data.get("deprecated"),
            )

        # Static stubs can only help when the object exists but couldn't be inspected.
        # Never use them to overrule a definite METHOD_NOT_FOUND from the target sandbox.
        if status not in _NO_STUB_FALLBACK:
            stub = self.stub_provider.resolve(python_exec, package_name, method_name)
            if stub is not None:
                params, accepts_kwargs = stub
                return SignatureDefinition(package_name, method_name, params, is_unknown=False,
                                           accepts_kwargs=accepts_kwargs, error_code="SUCCESS",
                                           deprecated=data.get("deprecated"))

        return SignatureDefinition(package_name, method_name, [], is_unknown=True, error_code=status,
                                   deprecated=data.get("deprecated"))
