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


# A name in a scope is bound to one of:
#   ("import", (package, path))   e.g. `from openai import OpenAI`
#   ("instance", (package, path)) e.g. `client = OpenAI()`
#   ("other", None)               anything else: shadows outer bindings of the same name
Binding = Tuple[str, Any]
OTHER: Binding = ("other", None)

_IMPORT_GUARDS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


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
        # Scope stack, innermost last: (kind, {name: Binding}); kind is module/function/class
        self._scopes: List[Tuple[str, Dict[str, Binding]]] = [("module", {})]
        # Per class: {attr: Instance or None}, for self.<attr> inside its methods
        self._class_attrs: List[Dict[str, Optional[Instance]]] = []
        # Name of `self` in the current method (None outside methods)
        self._self_names: List[Optional[str]] = [None]
        # Module-level instances defined in THIS file (exported for cross-file tracing)
        self.module_instances: Dict[str, Instance] = {}
        self._guard_depth = 0        # > 0 inside `try:` blocks that catch ImportError
        self._statement_line = 0     # first line of the statement being visited

    # ---------- symbol table ----------

    def _bind(self, name: str, binding: Binding):
        kind, table = self._scopes[-1]
        table[name] = binding
        if kind == "module":
            if binding[0] == "instance":
                self.module_instances[name] = binding[1]
            else:
                self.module_instances.pop(name, None)

    def _lookup(self, name: str) -> Optional[Binding]:
        # Like Python: class bodies are not visible from inside their methods
        for i in range(len(self._scopes) - 1, -1, -1):
            kind, table = self._scopes[i]
            if kind == "class" and i != len(self._scopes) - 1:
                continue
            if name in table:
                return table[name]
        return None

    def _import(self, name: str) -> Optional[Tuple[str, str]]:
        b = self._lookup(name)
        return b[1] if b and b[0] == "import" else None

    def _instance(self, name: str) -> Optional[Instance]:
        b = self._lookup(name)
        return b[1] if b and b[0] == "instance" else None

    def _self_attr(self, attr: str) -> Optional[Instance]:
        if not self._class_attrs:
            return None
        return self._class_attrs[-1].get(attr)

    # ---------- statements ----------

    def visit(self, node):
        if isinstance(node, ast.stmt):
            previous, self._statement_line = self._statement_line, node.lineno
            try:
                return super().visit(node)
            finally:
                self._statement_line = previous
        return super().visit(node)

    # ---------- imports ----------

    def _record_import_site(self, node: ast.AST, package: str, path: str):
        """Every imported name below the package root must still exist in the target version."""
        if path:
            self.call_sites.append(CallSite(
                file_path=self.file_path, line_number=getattr(node, "lineno", 0), package_name=package,
                method_name=path, kwargs_passed=[], has_dynamic_kwargs=False, kind="import",
                guarded=self._guard_depth > 0, statement_line=self._statement_line,
            ))

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            top, _, rest = alias.name.partition(".")
            if alias.asname:
                self._bind(alias.asname, ("import", (top, rest)))   # import a.b as c -> c is a.b
            else:
                self._bind(top, ("import", (top, "")))              # import a.b      -> name `a` is bound
            self._record_import_site(node, top, rest)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        source_module = self._resolve_from_module(node)
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            # `from svc.llm import client` where svc.llm defines `client = OpenAI()`
            exported = self.project_instances.get(source_module or "", {})
            if alias.name in exported:
                self._bind(local_name, ("instance", exported[alias.name]))
            elif node.level == 0 and node.module:
                top, _, rest = node.module.partition(".")
                path = ".".join(p for p in (rest, alias.name) if p)
                self._bind(local_name, ("import", (top, path)))
                self._record_import_site(node, top, path)
            else:
                self._bind(local_name, OTHER)  # project-local import
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
        if drop > len(base):
            return None
        base = base[: len(base) - drop] if drop > 0 else base
        parts = base + ([node.module] if node.module else [])
        return ".".join(p for p in parts if p)

    def _visit_try(self, node):
        guarded = any(
            h.type is None or self._exception_names(h.type) & _IMPORT_GUARDS for h in node.handlers
        )
        if guarded:
            self._guard_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        if guarded:
            self._guard_depth -= 1
        for part in (node.handlers, node.orelse, node.finalbody):
            for child in part:
                self.visit(child)

    visit_Try = _visit_try
    visit_TryStar = _visit_try

    @staticmethod
    def _exception_names(expr: ast.expr) -> set:
        nodes = expr.elts if isinstance(expr, ast.Tuple) else [expr]
        names = set()
        for n in nodes:
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
        return names

    # ---------- scopes ----------

    @staticmethod
    def _arg_names(args: ast.arguments) -> List[str]:
        names = [a.arg for a in getattr(args, "posonlyargs", []) + args.args + args.kwonlyargs]
        if args.vararg:
            names.append(args.vararg.arg)
        if args.kwarg:
            names.append(args.kwarg.arg)
        return names

    def _visit_function(self, node):
        # Decorators, defaults and annotations are evaluated in the enclosing scope
        for expr in node.decorator_list + node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(expr)
        in_class = self._scopes[-1][0] == "class"
        is_static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
        positional = [a.arg for a in getattr(node.args, "posonlyargs", []) + node.args.args]
        self_name = positional[0] if in_class and not is_static and positional else None

        self._scopes.append(("function", {}))
        self._self_names.append(self_name if in_class else self._self_names[-1])
        for name in self._arg_names(node.args):
            self._bind(name, OTHER)  # parameters shadow imports/instances with the same name
        for stmt in node.body:
            self.visit(stmt)
        self._self_names.pop()
        self._scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda):
        for expr in node.args.defaults + [d for d in node.args.kw_defaults if d]:
            self.visit(expr)
        self._scopes.append(("function", {}))
        for name in self._arg_names(node.args):
            self._bind(name, OTHER)
        self.visit(node.body)
        self._scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        for expr in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(expr)
        self._class_attrs.append(self._collect_self_attrs(node))
        self._scopes.append(("class", {}))
        for stmt in node.body:
            self.visit(stmt)
        self._scopes.pop()
        self._class_attrs.pop()
        self._bind(node.name, OTHER)

    def _collect_self_attrs(self, cls: ast.ClassDef) -> Dict[str, Optional[Instance]]:
        """
        self.<attr> assignments in all methods of the class, collected up front so methods
        can use attributes set in __init__ wherever they appear. An attribute assigned
        anything that is not a package constructor is not tracked (None).
        """
        attrs: Dict[str, Optional[Instance]] = {}
        for method in cls.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = [a.arg for a in getattr(method.args, "posonlyargs", []) + method.args.args]
            if not positional:
                continue
            self_name = positional[0]
            for node in ast.walk(method):
                targets, value = [], None
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets, value = [node.target], node.value
                for t in targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == self_name):
                        instance = self._instance_from_constructor(value) if value is not None else None
                        if t.attr in attrs and attrs[t.attr] != instance:
                            attrs[t.attr] = None  # assigned different things: ambiguous
                        else:
                            attrs[t.attr] = instance
        return attrs

    def _visit_comprehension(self, node):
        # Comprehension variables live in their own scope and shadow outer names
        self._scopes.append(("function", {}))
        for gen in node.generators:
            for target in self._target_names(gen.target):
                self._bind(target, OTHER)
        self.generic_visit(node)
        self._scopes.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    # ---------- instance tracking ----------

    @staticmethod
    def _join(*parts: str) -> str:
        return ".".join(p for p in parts if p)

    @staticmethod
    def _target_names(target: ast.expr) -> List[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            return [n for elt in target.elts for n in CallSiteVisitor._target_names(elt)]
        if isinstance(target, ast.Starred):
            return CallSiteVisitor._target_names(target.value)
        return []

    def _instance_from_constructor(self, node: Optional[ast.expr]) -> Optional[Instance]:
        """OpenAI(...) / openai.OpenAI(...) / azure.AzureOpenAI(...) -> ("openai", "<path>.Class")"""
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if isinstance(func, ast.Name):
            imported = self._import(func.id)
            if imported and imported[1] and imported[1].split(".")[-1][:1].isupper():
                return imported
        elif isinstance(func, ast.Attribute):
            base, parts = _attribute_chain(func)
            if isinstance(base, ast.Name) and parts[-1][:1].isupper():
                imported = self._import(base.id)
                if imported:
                    package, path = imported
                    return package, self._join(path, *parts)
        return None

    def _assign(self, targets: List[ast.expr], value: Optional[ast.expr]):
        instance = self._instance_from_constructor(value) if value is not None else None
        single = len(targets) == 1 and isinstance(targets[0], ast.Name)
        for target in targets:
            for name in self._target_names(target):
                # Only `x = Ctor()` is tracked; tuple unpacking etc. just shadow
                self._bind(name, ("instance", instance) if instance and single else OTHER)

    def visit_Assign(self, node: ast.Assign):
        self.visit(node.value)
        self._assign(node.targets, node.value)
        for t in node.targets:
            if not isinstance(t, ast.Name):
                self.visit(t)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is not None:
            self.visit(node.value)
            self._assign([node.target], node.value)
        if not isinstance(node.target, ast.Name):
            self.visit(node.target)

    def visit_AugAssign(self, node: ast.AugAssign):
        self.generic_visit(node)
        for name in self._target_names(node.target):
            self._bind(name, OTHER)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self._assign([node.target], node.value)

    def _visit_for(self, node):
        self.visit(node.iter)
        for name in self._target_names(node.target):
            self._bind(name, OTHER)
        for part in (node.body, node.orelse):
            for stmt in part:
                self.visit(stmt)

    visit_For = _visit_for
    visit_AsyncFor = _visit_for

    def _visit_with(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._assign([item.optional_vars], item.context_expr)
        for stmt in node.body:
            self.visit(stmt)

    visit_With = _visit_with
    visit_AsyncWith = _visit_with

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._bind(node.name, OTHER)
        for stmt in node.body:
            self.visit(stmt)

    # ---------- calls ----------

    def visit_Call(self, node: ast.Call):
        resolved = self._resolve_call(node.func)
        if resolved:
            package_name, method_name, kind = resolved
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
                    positional_count=sum(1 for a in node.args if not isinstance(a, ast.Starred)),
                    has_star_args=any(isinstance(a, ast.Starred) for a in node.args),
                    kind=kind,
                    statement_line=self._statement_line,
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _is_class_name(name: str) -> bool:
        return name[:1].isupper()

    def _resolve_call(self, func: ast.expr) -> Optional[Tuple[str, str, str]]:
        """Returns (package, dotted path inside the package, kind) for calls into a package."""
        # fn(...) / Client(...) where `from pkg import fn, Client`
        if isinstance(func, ast.Name):
            imported = self._import(func.id)
            if imported and imported[1]:
                package, path = imported
                kind = "constructor" if self._is_class_name(path.split(".")[-1]) else "call"
                return package, path, kind
            return None

        if not isinstance(func, ast.Attribute):
            return None
        base, parts = _attribute_chain(func)
        instance: Optional[Instance] = None

        if isinstance(base, ast.Name):
            self_name = self._self_names[-1]
            if base.id == self_name and parts and self._self_attr(parts[0]):
                # self.client.chat.completions.create(...)
                instance = self._self_attr(parts[0])
                parts = parts[1:]
            elif self._instance(base.id):
                # client.chat.completions.create(...)
                instance = self._instance(base.id)
            elif self._import(base.id):
                # openai.ChatCompletion.create(...) / t.fn(...) / mock_sdk.Client(...)
                package, path = self._import(base.id)
                kind = "constructor" if self._is_class_name(parts[-1]) else "call"
                return package, self._join(path, *parts), kind
            else:
                return None
        elif isinstance(base, ast.Call):
            # OpenAI().chat.completions.create(...)
            instance = self._instance_from_constructor(base)
        else:
            return None

        if not instance or not parts:
            return None
        package_name, class_path = instance
        return package_name, self._join(class_path, *parts), "call"


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
