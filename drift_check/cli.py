import os
import typer
from rich.console import Console
from packaging.utils import canonicalize_name, parse_wheel_filename, InvalidWheelFilename
from packaging.version import Version, InvalidVersion
from drift_check.env import EnvironmentDetector
from drift_check.dependencies import BaselineResolver, BaselineStatus , DependencyDeclarationResolver
from drift_check.pypi import PyPIResolver
from drift_check.scanner import ProjectScanner
from drift_check.analyzer import ASTAnalyzer
from drift_check.signatures import SignatureResolver, SandboxError
from drift_check.rules import RuleEngine
from drift_check.reporter import Reporter
import importlib.metadata

app = typer.Typer()
console = Console()

# Exit codes: 0 = safe (warnings allowed), 1 = breaking changes found, 2 = tool/setup error
EXIT_BREAKING = 1
EXIT_TOOL_ERROR = 2


def normalize_name(name: str) -> str:
    return canonicalize_name(name)


def validate_local_wheel(path: str, package: str, to_version: str) -> str:
    """Ensures the wheel exists and matches the package and --to version. Returns the wheel's version."""
    if not os.path.isfile(path):
        console.print(f"[bold red]❌ Error:[/bold red] Local wheel not found: {path}")
        raise typer.Exit(code=EXIT_TOOL_ERROR)
    try:
        wheel_name, wheel_version, _, _ = parse_wheel_filename(os.path.basename(path))
    except InvalidWheelFilename:
        console.print(f"[bold red]❌ Error:[/bold red] Not a valid wheel file name: {os.path.basename(path)}")
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    if wheel_name != normalize_name(package):
        console.print(
            f"[bold red]❌ Error:[/bold red] Wheel is for '{wheel_name}', but you asked to upgrade '{package}'."
        )
        raise typer.Exit(code=EXIT_TOOL_ERROR)
    try:
        matches = Version(to_version) == wheel_version
    except InvalidVersion:
        matches = False
    if not matches:
        console.print(
            f"[bold red]❌ Error:[/bold red] --to {to_version} does not match the wheel's version ({wheel_version})."
        )
        raise typer.Exit(code=EXIT_TOOL_ERROR)
    return str(wheel_version)


def version_callback(value: bool):
    if value:
        print(f"drift-check {importlib.metadata.version('py-drift-check')}")
        raise typer.Exit()
@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the installed Drift Check version."
    )
):
    """Drift Check: Proactive upgrade-impact analyzer."""
    pass

@app.command()
def upgrade(
    package: str = typer.Argument(..., help="The package to analyze (e.g., openai)"),
    to_version: str = typer.Option(
        ...,
        "--to",
        help="Target version to check against (e.g., 15)"
    ),
    local_wheel: str = typer.Option(
        None,
        "--local-wheel",
        help="Local wheel to use instead of downloading the target package from PyPI"
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show detailed diagnostic information."
    )
):
    console.print(f"[bold blue]Starting Upgrade Analysis for {package} -> {to_version}...[/bold blue]")
    
    # 1. Environment Discovery
    env_detector = EnvironmentDetector(".")
    project_python = env_detector.find_python_executable()
    
    # 2. Baseline Resolution
    console.print("[dim]Verifying environment baseline...[/dim]")
    
    # NEW: Extract the real declaration from requirements.txt
    dec_resolver = DependencyDeclarationResolver(".")
    declared_specifier = dec_resolver.find(package)
    
    if not declared_specifier:
        console.print(f"[bold red]❌ Error:[/bold red] Package '{package}' is not declared in requirements.txt.")
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    baseline_resolver = BaselineResolver(project_python)
    baseline = baseline_resolver.resolve(package, declared_specifier)
    
    # Strict Halting Logic
    if baseline.status == BaselineStatus.NOT_INSTALLED:
        console.print(f"[bold red]❌ Error:[/bold red] Package '{package}' is not installed in the target environment.")
        raise typer.Exit(code=EXIT_TOOL_ERROR)
        
    elif baseline.status == BaselineStatus.INVALID_DECLARATION:
        console.print(f"[bold red]❌ Error:[/bold red] Unparsable requirement declaration for '{package}'.")
        raise typer.Exit(code=EXIT_TOOL_ERROR)
        
    elif baseline.status == BaselineStatus.MISMATCH:
        console.print(
            f"[bold red]❌ Error: Environment mismatch.[/bold red]\n"
            f"  Declared:  {baseline.declared_specifier}\n"
            f"  Installed: {baseline.installed_version}\n"
            f"Please sync your environment before analyzing upgrades."
        )
        raise typer.Exit(code=EXIT_TOOL_ERROR)
        
    # Execution continues only on MATCH
    console.print(f"[green]✓ Baseline established:[/green] {package} == {baseline.baseline_version}")

    # 3. AST Scanning (before any network/sandbox work)
    console.print("[dim]Scanning codebase for API calls...[/dim]")
    scanner = ProjectScanner(".")
    py_files, _ = scanner.scan()
    normalized_target_pkg = normalize_name(package)
    call_sites = [
        call for call in ASTAnalyzer(py_files).analyze()
        if normalize_name(call.package_name) == normalized_target_pkg
    ]

    if not call_sites:
        console.print(f"[green]✓ No calls to '{package}' found in this project. Nothing to check.[/green]")
        return

    # 4. Target Normalization (PyPI) or Local Wheel Validation
    if local_wheel:
        resolved_target = validate_local_wheel(local_wheel, package, to_version)
        console.print(f"\n[dim]Bypassing PyPI, using local wheel {local_wheel}...[/dim]")
    else:
        console.print(f"\n[dim]Resolving target version '{to_version}' on PyPI...[/dim]")
        pypi_resolver = PyPIResolver(package)
        resolved_target = pypi_resolver.resolve(to_version)

    console.print(f"[green]✓ Target resolved:[/green] {resolved_target}")

    # 5. Target API Resolution & Sandboxing
    console.print(f"\n[dim]Building isolated sandbox for {package}=={resolved_target}...[/dim]")
    try:
        sig_resolver = SignatureResolver(
            project_python_exec=project_python,
            target_version=resolved_target,
            package_to_sandbox=package,
            local_wheel=local_wheel,
            verbose=verbose,
        )
    except SandboxError as e:
        console.print(f"[bold red]❌ Error:[/bold red] {e}")
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    console.print(f"[dim]Checking {len(call_sites)} call site(s)...[/dim]")
    rule_engine = RuleEngine()
    all_findings = []
    for call in call_sites:
        sig = sig_resolver.resolve(call.package_name, call.method_name)
        all_findings.extend(rule_engine.evaluate(call, sig))

    # 6. Report Generation
    reporter = Reporter()
    reporter.generate_console_report(all_findings)
    # reporter.generate_json_report(all_findings) # Optional based on CLI flags
    
    # 7. CI/CD Exit Code Enforcement
    has_breaking_changes = any(f.severity == "ERROR" for f in all_findings)
    if has_breaking_changes:
        console.print("\n[bold red]Build Failed:[/bold red] Incompatible API calls detected in target version.")
        raise typer.Exit(code=EXIT_BREAKING)

if __name__ == "__main__":
    app()