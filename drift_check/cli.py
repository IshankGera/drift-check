import typer
import sys
from rich.console import Console
from drift_check.env import EnvironmentDetector
from drift_check.dependencies import BaselineResolver, BaselineStatus , DependencyDeclarationResolver
from drift_check.pypi import PyPIResolver
# Add the missing imports:
from drift_check.scanner import ProjectScanner
from drift_check.analyzer import ASTAnalyzer
from drift_check.signatures import SignatureResolver
from drift_check.rules import RuleEngine
from drift_check.reporter import Reporter
import importlib.metadata

app = typer.Typer()
console = Console()


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
        raise typer.Exit(code=1)

    baseline_resolver = BaselineResolver(project_python)
    baseline = baseline_resolver.resolve(package, declared_specifier)
    
    # Strict Halting Logic
    if baseline.status == BaselineStatus.NOT_INSTALLED:
        console.print(f"[bold red]❌ Error:[/bold red] Package '{package}' is not installed in the target environment.")
        raise typer.Exit(code=1)
        
    elif baseline.status == BaselineStatus.INVALID_DECLARATION:
        console.print(f"[bold red]❌ Error:[/bold red] Unparsable requirement declaration for '{package}'.")
        raise typer.Exit(code=1)
        
    elif baseline.status == BaselineStatus.MISMATCH:
        console.print(
            f"[bold red]❌ Error: Environment mismatch.[/bold red]\n"
            f"  Declared:  {baseline.declared_specifier}\n"
            f"  Installed: {baseline.installed_version}\n"
            f"Please sync your environment before analyzing upgrades."
        )
        raise typer.Exit(code=1)
        
    # Execution continues only on MATCH
    console.print(f"[green]✓ Baseline established:[/green] {package} == {baseline.baseline_version}")

    # 3. PyPI Target Normalization (or Bypass for Local Wheels)
    if local_wheel:
        console.print(f"\n[dim]Bypassing PyPI, using local wheel for version '{to_version}'...[/dim]")
        resolved_target = to_version
    else:
        console.print(f"\n[dim]Resolving target version '{to_version}' on PyPI...[/dim]")
        pypi_resolver = PyPIResolver(package)
        resolved_target = pypi_resolver.resolve(to_version)
        
    console.print(f"[green]✓ Target resolved:[/green] {resolved_target}")

    # 4. Target API Resolution & Sandboxing
    console.print(f"\n[dim]Building isolated sandbox for {package}=={resolved_target}...[/dim]")
    sig_resolver = SignatureResolver(
    project_python_exec=project_python,
    target_version=resolved_target,
    package_to_sandbox=package,
    local_wheel=local_wheel,
    verbose=verbose
    )

    # 5. AST Scanning & Rule Evaluation
    console.print("[dim]Scanning codebase for API calls...[/dim]")
    scanner = ProjectScanner(".")
    py_files, _ = scanner.scan()
    call_sites = ASTAnalyzer(py_files).analyze()
    
    rule_engine = RuleEngine()
    all_findings = []
    
    for call in call_sites:
        # Normalize both strings to use hyphens for a safe comparison
        normalized_call_pkg = call.package_name.replace("_", "-").lower()
        normalized_target_pkg = package.replace("_", "-").lower()
        
        # We only care about the package we are explicitly upgrading
        if normalized_call_pkg != normalized_target_pkg:
            continue
            
        sig = sig_resolver.resolve(call.package_name, call.method_name)
        findings = rule_engine.evaluate(call, sig)
        all_findings.extend(findings)
        
    # 6. Report Generation
    reporter = Reporter()
    reporter.generate_console_report(all_findings)
    # reporter.generate_json_report(all_findings) # Optional based on CLI flags
    
    # 7. CI/CD Exit Code Enforcement
    has_breaking_changes = any(f.severity == "ERROR" for f in all_findings)
    if has_breaking_changes:
        console.print("\n[bold red]Build Failed:[/bold red] Incompatible API calls detected in target version.")
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()