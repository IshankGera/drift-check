import sys # NEW
import typer
from typing import Optional
from rich.console import Console
from drift_check.scanner import ProjectScanner
from drift_check.dependencies import DependencyResolver
from drift_check.analyzer import ASTAnalyzer
from drift_check.signatures import SignatureResolver
from drift_check.rules import RuleEngine
from drift_check.reporter import Reporter
from drift_check.env import EnvironmentDetector

app = typer.Typer()
console = Console()

@app.callback()
def main():
    """Drift Check: Detect API signature drift."""
    pass

@app.command()
def run(
    target_dir: str = typer.Argument(".", help="Directory to scan"),
    package: Optional[str] = typer.Option(None, "--package", help="Filter analysis to a specific package"),
    to_version: Optional[str] = typer.Option(None, "--to", help="Target version to check against"),
    include_stdlib: bool = typer.Option(False, "--include-stdlib", help="Include standard library packages in analysis") # NEW
):
    console.print("[bold blue]Starting Drift Check...[/bold blue]")
    
    if to_version and not package:
        console.print("[red]❌ Error: You must specify a --package when using --to[/red]")
        raise typer.Exit(code=1)
    
    # 1. Environment Discovery
    env_detector = EnvironmentDetector(target_dir)
    project_python = env_detector.find_python_executable()
    console.print(f"[dim]Target Environment: {project_python}[/dim]")
    
    scanner = ProjectScanner(target_dir)
    py_files, req_files = scanner.scan()
    
    analyzer = ASTAnalyzer(py_files)
    call_sites = analyzer.analyze()
    
    console.print("\n[bold]Resolving Signatures...[/bold]")
    if to_version:
        console.print(f"[dim]Building isolated sandbox for {package}=={to_version}...[/dim]")
        
    # Inject the discovered project interpreter into the SignatureResolver
    sig_resolver = SignatureResolver(
        project_python_exec=project_python,
        target_version=to_version, 
        package_to_sandbox=package
    )
    
    rule_engine = RuleEngine()
    all_findings = []
    
    # Determine the stdlib names (fallback for older Python versions just in case)
    stdlib_names = sys.stdlib_module_names if hasattr(sys, 'stdlib_module_names') else set()
    
    for call in call_sites:
        # Skip self-references
        if call.package_name == "drift_check":
            continue
            
        # Filter by package if the user requested it
        if package and call.package_name != package:
            continue
            
        # NEW: Stdlib filtering
        if not include_stdlib and call.package_name in stdlib_names:
            continue
            
        sig = sig_resolver.resolve(call.package_name, call.method_name)
        findings = rule_engine.evaluate(call, sig)
        all_findings.extend(findings)
        
    reporter = Reporter()
    reporter.generate_console_report(all_findings)
    reporter.generate_json_report(all_findings)

if __name__ == "__main__":
    app()