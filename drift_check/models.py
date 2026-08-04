from dataclasses import dataclass
from typing import Optional, List

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

@dataclass
class SignatureDefinition:
    package_name: str
    method_name: str
    valid_parameters: List[str]
    is_unknown: bool = False
    accepts_kwargs: bool = False
    error_code: str = "SUCCESS"  
    
@dataclass
class Finding:
    severity: str
    file_path: str
    line_number: int
    package_name: str
    method_name: str
    message: str