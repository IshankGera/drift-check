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

    def report(self, findings: List[Finding], label: str = "Upgrade analysis", json_path: Optional[str] = None):
        """Console table, plus GitHub annotations in Actions, plus an optional JSON file."""
        self.generate_console_report(findings, label=label)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            self.github_annotations(findings)
        if json_path:
            self.generate_json_report(findings, json_path, label=label)

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
