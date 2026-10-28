import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from drift_check.models import CallSite

# (package_name, class_name), e.g. ("openai", "OpenAI")
Instance = Tuple[str, str]

_NO_LITERAL = object()


def _literal(node: ast.expr) -> Any:
    """Returns the Python value of a literal expression, or _NO_LITERAL."""
    try:
        return ast.literal_eval(node)
    except Exception:
        return _NO_LITERAL


def _attribute_chain(node: ast.expr) -> Tuple[Optional[ast.expr], List[str]]:
    """
    Attribute(Attribute(X, 'chat'), 'create') -> (X, ['chat', 'create']).
    Returns the innermost non-attribute expression and the attribute names.
    """
    parts: List[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.insert(0, current.attr)
        current = current.value
    return current, parts


def module_name_for(path: Path, root: Path) -> Optional[str]:
    """D:/proj/svc/llm.py -> 'svc.llm'; D:/proj/svc/__init__.py -> 'svc'."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


class CallSiteVisitor(ast.NodeVisitor):
    def __init__(
        self,
        file_path: str,
        module_name: Optional[str] = None,
        project_instances: Optional[Dict[str, Dict[str, Instance]]] = None,
    ):
        self.file_path = file_path
        self.module_name = module_name
        # {"svc.llm": {"client": ("openai", "OpenAI")}} - module-level clients across the project
        self.project_instances = project_instances or {}

        self.call_sites: List[CallSite] = []
        # Local name -> top-level package. {"OpenAI": "openai", "openai": "openai"}
        self.imports: Dict[str, str] = {}
        # Variable -> instance. {"client": ("openai", "OpenAI")}
        self.assignments: Dict[str, Instance] = {}
        # self.<attr> -> instance. {"client": ("openai", "OpenAI")}
        self.self_assignments: Dict[str, Instance] = {}
        # Module-level instances defined in THIS file (exported for cross-file tracing)
        self.module_instances: Dict[str, Instance] = {}
        self._depth = 0  # 0 = module level

    # ---------- imports ----------

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            local_name = alias.asname or alias.name.split(".")[0]
            self.imports[local_name] = alias.name.split(".")[0]
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        source_module = self._resolve_from_module(node)
        for alias in node.names:
            local_name = alias.asname or alias.name
            # `from svc.llm import client` where svc.llm defines `client = OpenAI()`
            exported = self.project_instances.get(source_module or "", {})
            if alias.name in exported:
                self.assignments[local_name] = exported[alias.name]
            elif node.level == 0 and node.module:
                self.imports[local_name] = node.module.split(".")[0]
        self.generic_visit(node)

    def _resolve_from_module(self, node: ast.ImportFrom) -> Optional[str]:
        if node.level == 0:
            return node.module
        if not self.module_name:
            return None
        # Relative import: walk up from the current module's package
        base = self.module_name.split(".")
        is_package = self.file_path.endswith("__init__.py")
        drop = node.level - (1 if is_package else 0)
        base = base[: len(base) - drop] if drop > 0 else base
        if drop > len(self.module_name.split(".")):
            return None
        parts = base + ([node.module] if node.module else [])
        return ".".join(p for p in parts if p)

    # ---------- scopes ----------

    def _visit_scope(self, node):
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    # ---------- instance tracking ----------

    def _instance_from_constructor(self, node: ast.expr) -> Optional[Instance]:
        """OpenAI(...) / openai.OpenAI(...) -> ("openai", "OpenAI")"""
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if isinstance(func, ast.Name):
            package = self.imports.get(func.id)
            if package and func.id[:1].isupper():
                return package, func.id
        elif isinstance(func, ast.Attribute):
            base, parts = _attribute_chain(func)
            if isinstance(base, ast.Name) and base.id in self.imports and parts[-1][:1].isupper():
                return self.imports[base.id], parts[-1]
        return None

    def _record_assignment(self, target: ast.expr, instance: Instance):
        if isinstance(target, ast.Name):
            self.assignments[target.id] = instance
            if self._depth == 0:
                self.module_instances[target.id] = instance
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            self.self_assignments[target.attr] = instance

    def visit_Assign(self, node: ast.Assign):
        instance = self._instance_from_constructor(node.value)
        if instance:
            for target in node.targets:
                self._record_assignment(target, instance)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is not None:
            instance = self._instance_from_constructor(node.value)
            if instance:
                self._record_assignment(node.target, instance)
        self.generic_visit(node)

    # ---------- calls ----------

    def visit_Call(self, node: ast.Call):
        resolved = self._resolve_call(node.func)
        if resolved:
            package_name, method_name = resolved
            kwargs, literals, has_dynamic = [], {}, False
            for kw in node.keywords:
                if kw.arg is None:
                    has_dynamic = True
                    continue
                kwargs.append(kw.arg)
                value = _literal(kw.value)
                if value is not _NO_LITERAL:
                    literals[kw.arg] = value
            self.call_sites.append(
                CallSite(
                    file_path=self.file_path,
                    line_number=node.lineno,
                    package_name=package_name,
                    method_name=method_name,
                    kwargs_passed=kwargs,
                    has_dynamic_kwargs=has_dynamic,
                    literal_kwargs=literals,
                )
            )
        self.generic_visit(node)

    def _resolve_call(self, func: ast.expr) -> Optional[Tuple[str, str]]:
        if not isinstance(func, ast.Attribute):
            return None
        base, parts = _attribute_chain(func)
        instance: Optional[Instance] = None

        if isinstance(base, ast.Name):
            if base.id == "self" and parts and parts[0] in self.self_assignments:
                # self.client.chat.completions.create(...)
                instance = self.self_assignments[parts[0]]
                parts = parts[1:]
            elif base.id in self.assignments:
                # client.chat.completions.create(...)
                instance = self.assignments[base.id]
            elif base.id in self.imports:
                # openai.ChatCompletion.create(...) / mock_sdk.create(...)
                # Skip bare class constructors like mock_sdk.Client()
                if len(parts) == 1 and parts[0][:1].isupper():
                    return None
                return self.imports[base.id], ".".join(parts)
            else:
                return None
        elif isinstance(base, ast.Call):
            # OpenAI().chat.completions.create(...)
            instance = self._instance_from_constructor(base)
        else:
            return None

        if not instance or not parts:
            return None
        package_name, class_name = instance
        return package_name, ".".join([class_name] + parts)


class ASTAnalyzer:
    def __init__(self, python_files: List[Path], root: Optional[Path] = None):
        self.python_files = python_files
        self.root = Path(root) if root else Path(".")

    def _parse(self, file_path: Path) -> Optional[ast.AST]:
        try:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                return ast.parse(f.read(), filename=str(file_path))
        except Exception:
            return None  # Unparsable files are skipped

    def analyze(self) -> List[CallSite]:
        trees = {fp: self._parse(fp) for fp in self.python_files}
        modules = {fp: module_name_for(fp, self.root) for fp in self.python_files}

        # Pass 1: collect module-level clients, e.g. svc/llm.py: client = OpenAI()
        project_instances: Dict[str, Dict[str, Instance]] = {}
        for fp, tree in trees.items():
            if tree is None or not modules[fp]:
                continue
            v = CallSiteVisitor(str(fp), modules[fp])
            v.visit(tree)
            if v.module_instances:
                project_instances[modules[fp]] = v.module_instances

        # Pass 2: collect call sites with cross-file knowledge
        all_calls: List[CallSite] = []
        for fp, tree in trees.items():
            if tree is None:
                continue
            v = CallSiteVisitor(str(fp), modules[fp], project_instances)
            v.visit(tree)
            all_calls.extend(v.call_sites)
        return all_calls
