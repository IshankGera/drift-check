#analyzer.py
import ast
from pathlib import Path
from typing import List, Dict, Any
from drift_check.models import CallSite

class CallSiteVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.call_sites: List[CallSite] = []
        
        # Tracks: {"OpenAI": "openai"}
        self.imports: Dict[str, str] = {}
        # Tracks: {"client": "OpenAI"}
        self.assignments: Dict[str, tuple[str, str]] = {}

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            local_name = alias.asname or alias.name.split('.')[0]
            self.imports[local_name] = alias.name.split('.')[0]

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            for alias in node.names:
                local_name = alias.asname or alias.name
                self.imports[local_name] = node.module.split('.')[0]
        self.generic_visit(node)


    def visit_Assign(self, node: ast.Assign):
        # Track:
        # client = mock_sdk.Client()
        if isinstance(node.value, ast.Call):
            func = node.value.func

            if isinstance(func, ast.Attribute):
                parts = self._resolve_attribute_chain(func)

                if len(parts) >= 2:
                    # Example:
                    # mock_sdk.Client()
                    #
                    # parts = ["mock_sdk", "Client"]

                    package_name = self.imports.get(parts[0])

                    if package_name:
                        class_name = parts[-1]

                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                self.assignments[target.id] = (
                                    package_name,
                                    class_name
                                )

            elif isinstance(func, ast.Name):
                # Example:
                # client = Client()
                class_name = func.id
                package_name = self.imports.get(class_name)

                if package_name:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.assignments[target.id] = (
                                package_name,
                                class_name
                            )

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Attribute):
            parts = self._resolve_attribute_chain(node.func)

            if parts:
                base_var = parts[0]
                method_chain = ".".join(parts[1:])

                # Resolve:
                # client -> ("mock_sdk", "Client")
                assignment = self.assignments.get(base_var)

                if assignment:
                    package_name, class_name = assignment
                    method_name = f"{class_name}.{method_chain}"
                else:
                    package_name = self.imports.get(base_var)
                    method_name = method_chain

                if package_name:
                    kwargs = []
                    has_dynamic = False

                    for kw in node.keywords:
                        if kw.arg is None:
                            has_dynamic = True
                        else:
                            kwargs.append(kw.arg)

                    self.call_sites.append(
                        CallSite(
                            file_path=self.file_path,
                            line_number=node.lineno,
                            package_name=package_name,
                            method_name=method_name,
                            kwargs_passed=kwargs,
                            has_dynamic_kwargs=has_dynamic
                        )
                    )

        self.generic_visit(node)

    def _resolve_attribute_chain(self, node: ast.expr) -> List[str]:
        """Unpacks Attribute(Attribute(Name('client'), 'chat'), 'create') -> ['client', 'chat', 'create']"""
        parts = []
        current = node
        while isinstance(current, ast.Attribute):
            parts.insert(0, current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.insert(0, current.id)
            return parts
        return []

class ASTAnalyzer:
    def __init__(self, python_files: List[Path]):
        self.python_files = python_files

    def analyze(self) -> List[CallSite]:
        all_calls = []
        for file_path in self.python_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=str(file_path))
                visitor = CallSiteVisitor(str(file_path))
                visitor.visit(tree)
                all_calls.extend(visitor.call_sites)
            except Exception as e:
                pass # Ignore syntax errors in V1
        return all_calls