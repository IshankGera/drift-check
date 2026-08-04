import json
from typing import List
from rich.console import Console
from rich.table import Table
from drift_check.models import Finding

class Reporter:
    def __init__(self):
        self.console = Console()

    def generate_console_report(self, findings: List[Finding]):
        if not findings:
            self.console.print("[green]No findings to report![/green]")
            return

        self.console.print("\n[bold]Drift Check Results:[/bold]")
        
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Status", width=10)
        table.add_column("Location")
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
            
            table.add_row(
                status,
                f"{short_path}:{finding.line_number}",
                finding.package_name,
                finding.message
            )
        
        self.console.print(table)

    def generate_json_report(self, findings: List[Finding], output_path: str = "report.json"):
        data = [
            {
                "severity": f.severity,
                "file_path": f.file_path,
                "line_number": f.line_number,
                "package_name": f.package_name,
                "method_name": f.method_name,
                "message": f.message
            }
            for f in findings
        ]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.console.print(f"\n[dim]Report successfully saved to {output_path}[/dim]")