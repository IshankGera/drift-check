from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Dependency:
    name: str
    installed_version: Optional[str]
    is_pinned: bool
    specifier: str


@dataclass
class CallSite:
    file_path: str
    line_number: int
    package_name: str
    method_name: str
    kwargs_passed: List[str]
    has_dynamic_kwargs: bool
    # Keyword arguments whose value is a plain literal in the source,
    # e.g. model="o3-mini" -> {"model": "o3-mini"}. Used by `drift-check probe`.
    literal_kwargs: Dict[str, Any] = field(default_factory=dict)
    # "call" for a function/method call, "import" for `from pkg.x import name` / `import pkg.x`
    kind: str = "call"
    # Positional arguments at the call site, and whether one of them is *args
    positional_count: int = 0
    has_star_args: bool = False
    # Import inside `try: ... except ImportError:` (an optional dependency or version fallback)
    guarded: bool = False
    # First line of the enclosing statement (coverage reports statements by their first line)
    statement_line: int = 0


@dataclass
class SignatureDefinition:
    package_name: str
    method_name: str
    valid_parameters: List[str]
    is_unknown: bool = False
    accepts_kwargs: bool = False
    error_code: str = "SUCCESS"
    # Argument shape, used for required/positional checks (empty when unknown)
    positional_names: List[str] = field(default_factory=list)
    max_positional: Optional[int] = None          # None = unlimited (*args) or unknown
    required_params: List[str] = field(default_factory=list)
    # Deprecation message from the target version, if the API is marked deprecated
    deprecated: Optional[str] = None


@dataclass
class Finding:
    severity: str
    file_path: str
    line_number: int
    package_name: str
    method_name: str
    message: str
    kind: str = "call"  # "call", "constructor" or "import" (what produced the finding)
