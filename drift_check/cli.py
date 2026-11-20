import os
import sys
import shlex
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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
from drift_check import probe as probe_mod
from drift_check import testrun, typecheck as typecheck_mod
from drift_check.fullenv import ProjectSandbox, project_pythonpath
from drift_check.rules import RuleEngine, suppress_consequences
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
def establish_baseline(package: str) -> str:
    """
    Confirms the package is declared and that the installed version matches the declaration.
    Returns the project's Python executable. Exits with code 2 otherwise.
    """
    project_python = EnvironmentDetector(".").find_python_executable()
    console.print("[dim]Verifying environment baseline...[/dim]")

    declared_specifier = DependencyDeclarationResolver(".").find(package)
    if not declared_specifier:
        console.print(
            f"[bold red]❌ Error:[/bold red] Package '{package}' is not declared in "
            f"requirements.txt, pyproject.toml or uv.lock."
        )
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    baseline = BaselineResolver(project_python).resolve(package, declared_specifier)
    if baseline.status == BaselineStatus.NOT_INSTALLED:
        console.print(f"[bold red]❌ Error:[/bold red] Package '{package}' is not installed in the target environment.")
        raise typer.Exit(code=EXIT_TOOL_ERROR)
    if baseline.status == BaselineStatus.INVALID_DECLARATION:
        console.print(f"[bold red]❌ Error:[/bold red] Unparsable requirement declaration for '{package}'.")
        raise typer.Exit(code=EXIT_TOOL_ERROR)
    if baseline.status == BaselineStatus.MISMATCH:
        console.print(
            f"[bold red]❌ Error: Environment mismatch.[/bold red]\n"
            f"  Declared:  {baseline.declared_specifier}\n"
            f"  Installed: {baseline.installed_version}\n"
            f"Please sync your environment before analyzing upgrades."
        )
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    console.print(f"[green]✓ Baseline established:[/green] {package} == {baseline.baseline_version}")
    return project_python


def resolve_target(package: str, to_version: str, local_wheel: Optional[str]) -> str:
    """Normalizes --to against PyPI, or validates the local wheel. Returns the exact version."""
    if local_wheel:
        resolved = validate_local_wheel(local_wheel, package, to_version)
        console.print(f"\n[dim]Bypassing PyPI, using local wheel {local_wheel}...[/dim]")
    else:
        console.print(f"\n[dim]Resolving target version '{to_version}' on PyPI...[/dim]")
        resolved = PyPIResolver(package).resolve(to_version)
    console.print(f"[green]✓ Target resolved:[/green] {resolved}")
    return resolved


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
    """Drift Check: catch breaking API changes before you upgrade (upgrade) or deploy (probe)."""
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
    ),
    json_report: str = typer.Option(
        None, "--json-report", help="Also write the findings to this JSON file (for CI)."
    ),
):
    """
    Check your code against a future version of a package, before you upgrade.

    Flags methods and keyword arguments your code uses that no longer exist in the target version.
    """
    console.print(f"[bold blue]Starting Upgrade Analysis for {package} -> {to_version}...[/bold blue]")
    
    project_python = establish_baseline(package)

    # 3. AST Scanning (before any network/sandbox work)
    console.print("[dim]Scanning codebase for API calls...[/dim]")
    scanner = ProjectScanner(".")
    py_files, _ = scanner.scan()
    normalized_target_pkg = normalize_name(package)
    call_sites = [
        call for call in ASTAnalyzer(py_files, root=Path(".")).analyze()
        if normalize_name(call.package_name) == normalized_target_pkg
    ]

    if not call_sites:
        console.print(f"[green]✓ No calls to '{package}' found in this project. Nothing to check.[/green]")
        if json_report:
            Reporter().generate_json_report([], json_report)
        return

    resolved_target = resolve_target(package, to_version, local_wheel)

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

    n_imports = sum(1 for c in call_sites if c.kind == "import")
    console.print(f"[dim]Checking {len(call_sites) - n_imports} call(s) and {n_imports} import(s)...[/dim]")
    rule_engine = RuleEngine()
    all_findings = []
    for call in call_sites:
        sig = sig_resolver.resolve(call.package_name, call.method_name)
        baseline = None
        # Compare with the installed version only when the target shows a problem
        if RuleEngine.needs_baseline(call, sig):
            baseline = sig_resolver.resolve_baseline(call.package_name, call.method_name)
        all_findings.extend(rule_engine.evaluate(call, sig, baseline))
    all_findings = suppress_consequences(all_findings)

    # 6. Report Generation
    Reporter().report(all_findings, json_path=json_report)
    
    # 7. CI/CD Exit Code Enforcement
    has_breaking_changes = any(f.severity == "ERROR" for f in all_findings)
    if has_breaking_changes:
        console.print("\n[bold red]Build Failed:[/bold red] Incompatible API calls detected in target version.")
        raise typer.Exit(code=EXIT_BREAKING)

@app.command()
def probe(
    model: str = typer.Option(
        None, "--model", "-m",
        help="Model to test. Overrides model= literals in the code (needed when the model comes from config)."
    ),
    base_url: str = typer.Option(
        None, "--base-url",
        help="OpenAI-compatible API base URL. Defaults to $OPENAI_BASE_URL or https://api.openai.com/v1."
    ),
    api_key_env: str = typer.Option(
        "OPENAI_API_KEY", "--api-key-env",
        help="Name of the environment variable holding the API key."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the requests that would be sent, without sending anything."
    ),
    max_requests: int = typer.Option(
        20, "--max-requests", help="Safety cap on the number of unique requests sent."
    ),
    json_report: str = typer.Option(
        None, "--json-report", help="Also write the findings to this JSON file (for CI)."
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Check against built-in rules for known server changes. No API key, nothing sent."
    ),
    rules: str = typer.Option(
        None, "--rules", help="Extra rules file (JSON) for --offline, in the same format as the built-in rules."
    ),
):
    """
    Check your chat.completions.create() calls against the LIVE server (or known rules with --offline).

    Sends one tiny request per unique (model, parameters) combination, directly from
    this machine to the provider, using your own API key. Catches server-side rejections
    the Python SDK cannot see, e.g. 'max_tokens' not supported -> use 'max_completion_tokens'.
    """
    base_url = base_url or os.environ.get("OPENAI_BASE_URL") or probe_mod.DEFAULT_BASE_URL
    host = urlparse(base_url).netloc or base_url

    console.print("[bold blue]Probing OpenAI-compatible calls against the live server...[/bold blue]")
    console.print("[dim]Scanning codebase for chat.completions.create() calls...[/dim]")
    py_files, _ = ProjectScanner(".").scan()
    call_sites = ASTAnalyzer(py_files, root=Path(".")).analyze()

    if offline:
        try:
            rule_list = probe_mod.load_rules(rules)
        except (OSError, ValueError, KeyError) as e:
            console.print(f"[bold red]❌ Error:[/bold red] Could not load rules: {e}")
            raise typer.Exit(code=EXIT_TOOL_ERROR)
        findings = probe_mod.offline_findings(call_sites, rule_list, model_override=model)
        console.print(f"[dim]Checked against {len(rule_list)} offline rule(s). Nothing was sent.[/dim]")
        Reporter().report(findings, label="Offline rule check", json_path=json_report)
        if any(f.severity == "ERROR" for f in findings):
            console.print("\n[bold red]Build Failed:[/bold red] Calls match known server-side rejections.")
            raise typer.Exit(code=EXIT_BREAKING)
        return

    cases, unprobeable = probe_mod.build_cases(call_sites, model_override=model)

    findings = [
        probe_mod.Finding("WARNING", c.file_path, c.line_number, c.package_name, c.method_name, reason)
        for c, reason in unprobeable
    ]

    if not cases:
        reporter = Reporter()
        if findings:
            reporter.report(findings, label="Server probe", json_path=json_report)
        else:
            console.print("[green]✓ No chat.completions.create() calls found. Nothing to probe.[/green]")
        return

    if len(cases) > max_requests:
        console.print(
            f"[bold red]❌ Error:[/bold red] {len(cases)} unique requests needed, above --max-requests {max_requests}."
        )
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    if dry_run:
        console.print(f"\n[bold]Dry run:[/bold] {len(cases)} request(s) would be sent to {host}\n")
        for case in cases:
            sites = ", ".join(f"{Path(c.file_path).name}:{c.line_number}" for c in case.call_sites)
            console.print(f"  • model={case.model} params={case.params}  [dim]({sites})[/dim]")
            if case.skipped_params:
                console.print(f"    [yellow]not probed (no literal value):[/yellow] {', '.join(case.skipped_params)}")
        return

    api_key = os.environ.get(api_key_env)
    if not api_key:
        console.print(
            f"[bold red]❌ Error:[/bold red] ${api_key_env} is not set. "
            f"Set it, point --api-key-env at the variable holding your key, "
            f"or use --offline to check known rules without a key."
        )
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    console.print(f"[dim]Sending {len(cases)} minimal request(s) to {host} with your API key...[/dim]")
    for case in cases:
        for sent_case, result in probe_mod.run_case(base_url, api_key, case):
            if result.status == "AUTH_ERROR":
                console.print(
                    f"[bold red]❌ Error:[/bold red] {host} rejected your API key "
                    f"(HTTP {result.http_status}): {result.message}"
                )
                raise typer.Exit(code=EXIT_TOOL_ERROR)
            if result.status == "NETWORK_ERROR":
                console.print(f"[bold red]❌ Error:[/bold red] Could not reach {host}: {result.message}")
                raise typer.Exit(code=EXIT_TOOL_ERROR)
            findings.extend(probe_mod.findings_for(sent_case, result))

    Reporter().report(findings, label="Server probe", json_path=json_report)
    if any(f.severity == "ERROR" for f in findings):
        console.print("\n[bold red]Build Failed:[/bold red] The server rejects calls made by this code.")
        raise typer.Exit(code=EXIT_BREAKING)


@app.command()
def typecheck(
    package: str = typer.Argument(..., help="The package to analyze (e.g., pydantic)"),
    to_version: str = typer.Option(..., "--to", help="Target version to check against"),
    local_wheel: str = typer.Option(None, "--local-wheel", help="Local wheel to use instead of PyPI"),
    json_report: str = typer.Option(None, "--json-report", help="Also write the findings to this JSON file."),
):
    """
    Type-check your project before and after the upgrade, and report only NEW type errors.

    Catches what the call-site check cannot see, such as methods on your own subclasses of the
    package's classes. Needs your project's dependencies installed (they are copied to a sandbox).
    """
    console.print(f"[bold blue]Type-check diff for {package} -> {to_version}...[/bold blue]")
    project_python = establish_baseline(package)
    resolved_target = resolve_target(package, to_version, local_wheel)

    console.print("\n[dim]Building a sandbox with your project's packages and the new version "
                  "(this can take a minute)...[/dim]")
    sandbox = None
    try:
        sandbox = ProjectSandbox(project_python, package, resolved_target, local_wheel).build()
        findings = typecheck_mod.check(sandbox.uv, project_python, sandbox.python, Path(".").resolve(), package,
                                       log=lambda m: console.print(f"[dim]{m}[/dim]"))
    except (SandboxError, typecheck_mod.TypeCheckError) as e:
        console.print(f"[bold red]❌ Error:[/bold red] {e}")
        raise typer.Exit(code=EXIT_TOOL_ERROR)
    finally:
        if sandbox:
            sandbox.cleanup()

    Reporter().report(findings, label="Type-check diff", json_path=json_report)
    if any(f.severity == "ERROR" for f in findings):
        console.print("\n[bold red]Build Failed:[/bold red] The upgrade introduces new type errors.")
        raise typer.Exit(code=EXIT_BREAKING)


@app.command("test")
def test_command(
    package: str = typer.Argument(..., help="The package to analyze (e.g., openai)"),
    to_version: str = typer.Option(..., "--to", help="Target version to check against"),
    local_wheel: str = typer.Option(None, "--local-wheel", help="Local wheel to use instead of PyPI"),
    pytest_args: str = typer.Option("", "--pytest-args", help='Extra pytest arguments, e.g. "tests/unit -x"'),
    coverage_risk: bool = typer.Option(
        True, "--coverage-risk/--no-coverage-risk",
        help="Also list calls into the package that no test runs."
    ),
    json_report: str = typer.Option(None, "--json-report", help="Also write the findings to this JSON file."),
):
    """
    Run your tests with the upgraded package and compare with today.

    Reports tests that start failing, new deprecation warnings, and calls into the package
    that no test executes (where a break would go unnoticed).
    """
    console.print(f"[bold blue]Test run for {package} -> {to_version}...[/bold blue]")
    project_python = establish_baseline(package)
    resolved_target = resolve_target(package, to_version, local_wheel)
    root = Path(".").resolve()
    pythonpath = project_pythonpath(root)
    args = shlex.split(pytest_args)

    baseline_run = None
    if testrun.has_pytest(project_python):
        console.print("\n[dim]Running your tests with your current versions...[/dim]")
        baseline_run = testrun.run_pytest(project_python, root, pythonpath, args)
    else:
        console.print("\n[yellow]pytest is not installed in your environment, so there is no baseline run. "
                      "Failures are reported without checking whether they also fail today.[/yellow]")

    console.print("[dim]Building a sandbox with your project's packages and the new version...[/dim]")
    sandbox = None
    try:
        extras = ["pytest", "coverage"] if coverage_risk else ["pytest"]
        sandbox = ProjectSandbox(project_python, package, resolved_target, local_wheel, extras).build()
        console.print("[dim]Running your tests with the upgraded version...[/dim]")
        with tempfile.TemporaryDirectory(prefix="drift-check-cov-") as tmp:
            cov_file = Path(tmp) / ".coverage" if coverage_risk else None
            target_run = testrun.run_pytest(sandbox.python, root, pythonpath, args, coverage_file=cov_file)
            executed = testrun.coverage_json(sandbox.python, cov_file, root) if coverage_risk else None
    except SandboxError as e:
        console.print(f"[bold red]❌ Error:[/bold red] {e}")
        raise typer.Exit(code=EXIT_TOOL_ERROR)
    finally:
        if sandbox:
            sandbox.cleanup()

    if not target_run.ran:
        console.print("[bold red]❌ Error:[/bold red] pytest did not run in the sandbox.\n" + target_run.output)
        raise typer.Exit(code=EXIT_TOOL_ERROR)

    call_sites = []
    if coverage_risk:
        py_files, _ = ProjectScanner(".").scan()
        call_sites = [c for c in ASTAnalyzer(py_files, root=Path(".")).analyze()
                      if normalize_name(c.package_name) == normalize_name(package)]

    console.print(f"[dim]{target_run.total} test(s) run with the upgraded version"
                  + (f", {baseline_run.total} with your current version." if baseline_run else ".") + "[/dim]")
    findings = testrun.build_findings(package, baseline_run, target_run, root, call_sites, executed)
    Reporter().report(findings, label="Test run", json_path=json_report)
    if any(f.severity == "ERROR" for f in findings):
        console.print("\n[bold red]Build Failed:[/bold red] Tests break with the upgraded version.")
        raise typer.Exit(code=EXIT_BREAKING)


def _configure_streams():
    """
    Windows consoles and CI logs are often not UTF-8 (cp1252). Printing ✓/❌ there would
    crash, so unsupported characters are replaced instead.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def run():
    """Console entry point: like app(), but an unexpected crash exits 2 (tool error), never 1."""
    _configure_streams()
    try:
        app()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        import traceback
        traceback.print_exc()
        print("\ndrift-check crashed (this is a bug in drift-check, not in your code). "
              "Please report it at https://github.com/IshankGera/drift-check/issues", file=sys.stderr)
        sys.exit(EXIT_TOOL_ERROR)


if __name__ == "__main__":
    run()