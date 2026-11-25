import json
import os
from typing import List, Optional
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from drift_check.models import Finding

class Reporter:
    def __init__(self):
        self.console = Console()

    def generate_console_report(self, findings: List[Finding], label: str = "Upgrade analysis"):
        if not findings:
            self.console.print("[green]✓ No compatibility issues found.[/green]")
            self.console.print(f"[green]{label} passed.[/green]")
            return

        self.console.print("\n[bold]Drift Check Results:[/bold]")
        
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Status", width=10)
        table.add_column("Location", overflow="fold")
        table.add_column("Package")
        table.add_column("Message")

        for finding in findings:
            if finding.severity == "ERROR":
                status = "[bold red]❌ ERROR[/bold red]"
            elif finding.severity == "WARNING":
                status = "[bold yellow]⚠ WARN[/bold yellow]"
            else:
                status = "[bold green]✓ OK[/bold green]"
            
            # We only want to show the file name, not the massive absolute path
            short_path = finding.file_path.split("\\")[-1].split("/")[-1]
            location = f"{short_path}:{finding.line_number}" if finding.line_number > 0 else finding.file_path
            
            table.add_row(
                status,
                escape(location),
                escape(finding.package_name),
                escape(finding.message)
            )
        
        self.console.print(table)
        errors = sum(1 for f in findings if f.severity == "ERROR")
        warnings = sum(1 for f in findings if f.severity == "WARNING")

        if errors:
            self.console.print(
                f"\n[bold red]❌ {label} failed: "
                f"{errors} breaking issue(s) found.[/bold red]"
            )
        elif warnings:
            self.console.print(
                f"\n[bold yellow]⚠ {label} completed with "
                f"{warnings} warning(s).[/bold yellow]"
            )

    def report(self, findings: List[Finding], label: str = "Upgrade analysis", json_path: Optional[str] = None,
               sarif_path: Optional[str] = None, quiet: bool = False):
        """Console table, GitHub annotations in Actions, and optional JSON / SARIF files."""
        if not quiet:
            self.generate_console_report(findings, label=label)
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.github_annotations(findings)
        if json_path:
            self.generate_json_report(findings, json_path, label=label)
        if sarif_path:
            self.generate_sarif_report(findings, sarif_path)

    def generate_sarif_report(self, findings: List[Finding], output_path: str):
        """SARIF 2.1.0, the format GitHub code scanning (and most CI tools) read."""
        root = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
        rules, results = {}, []
        for f in findings:
            rule_id, description = rule_for(f)
            rules.setdefault(rule_id, description)
            file_part = f.file_path.split("::")[0]
            try:
                uri = os.path.relpath(file_part, root) if os.path.isabs(file_part) else file_part
            except ValueError:
                uri = file_part
            results.append({
                "ruleId": rule_id,
                "level": "error" if f.severity == "ERROR" else "warning",
                "message": {"text": f.message if "::" not in f.file_path else f"{f.file_path}: {f.message}"},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": uri.replace(os.sep, "/"), "uriBaseId": "%SRCROOT%"},
                        "region": {"startLine": max(1, f.line_number)},
                    }
                }],
                "partialFingerprints": {"driftCheck/v1": f"{rule_id}:{uri}:{f.package_name}:{f.method_name}"},
            })
        sarif = {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [{
                "tool": {"driver": {
                    "name": "drift-check",
                    "version": _version(),
                    "informationUri": "https://github.com/IshankGera/drift-check",
                    "rules": [
                        {"id": rid, "name": rid, "shortDescription": {"text": text}}
                        for rid, text in sorted(rules.items())
                    ],
                }},
                "results": results,
            }],
        }
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(sarif, fh, indent=2)
        self.console.print(f"[dim]SARIF report saved to {output_path}[/dim]")

    def github_annotations(self, findings: List[Finding]):
        """
        Workflow commands, so findings show up inline on the pull request's changed files.
        https://docs.github.com/actions/reference/workflow-commands-for-github-actions
        """
        root = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
        for f in findings:
            level = "error" if f.severity == "ERROR" else "warning"
            try:
                path = os.path.relpath(f.file_path, root).replace(os.sep, "/")
            except ValueError:
                path = f.file_path
            print(f"::{level} file={_escape_property(path)},line={f.line_number},"
                  f"title=drift-check ({_escape_property(f.package_name)})::{_escape_data(f.message)}")

    def generate_json_report(self, findings: List[Finding], output_path: str = "report.json",
                             label: str = "Upgrade analysis"):
        data = {
            "check": label,
            "summary": {
                "errors": sum(1 for f in findings if f.severity == "ERROR"),
                "warnings": sum(1 for f in findings if f.severity == "WARNING"),
            },
            "findings": [
                {
                    "severity": f.severity,
                    "file_path": f.file_path,
                    "line_number": f.line_number,
                    "package_name": f.package_name,
                    "method_name": f.method_name,
                    "kind": f.kind,
                    "message": f.message,
                }
                for f in findings
            ],
        }
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        self.console.print(f"\n[dim]Report saved to {output_path}[/dim]")


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


# (message prefix or fragment, rule id, description) - first match wins
_RULES = [
    ("Import '", "removed-import", "An imported name no longer exists in the target version"),
    ("Method '", "removed-api", "A function, class or method no longer exists in the target version"),
    ("is no longer supported", "removed-parameter", "A keyword argument is no longer accepted"),
    ("Required parameter", "missing-required-argument", "A parameter became required"),
    ("positional argument", "too-many-positional", "Fewer positional arguments are accepted"),
    ("Deprecat", "deprecated-api", "The API is deprecated in the target version"),
    ("New type error", "new-type-error", "The upgrade introduces a type error"),
    ("Test fails", "test-failure", "A test fails with the upgraded version"),
    ("Coverage risk", "untested-call", "A call into the package is not executed by any test"),
    ("Server (", "server-rejection", "The API server rejects the call"),
    ("offline rule", "known-server-rule", "The call matches a known server-side rejection"),
]


def rule_for(f: Finding):
    for fragment, rule_id, description in _RULES:
        if fragment in f.message:
            return rule_id, description
    return "unverifiable", "The call could not be verified against the target version"


def _version() -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version("py-drift-check")
    except Exception:
        return "0"
