import urllib.request
import json
import typer
from typing import List
from rich.console import Console
from packaging.version import Version, InvalidVersion

console = Console()

class PyPIResolver:
    def __init__(self, package_name: str):
        self.package_name = package_name

    def fetch_releases(self) -> List[str]:
        """Fetches all published versions of the package from PyPI."""
        url = f"https://pypi.org/pypi/{self.package_name}/json"
        try:
            with urllib.request.urlopen(url) as response:
                data = json.loads(response.read().decode())
                return list(data.get("releases", {}).keys())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                console.print(f"[bold red]❌ Error:[/bold red] Package '{self.package_name}' not found on PyPI.")
            else:
                console.print(f"[bold red]❌ Error:[/bold red] Failed to communicate with PyPI ({e.code}).")
            raise typer.Exit(code=1)
        except Exception as e:
            console.print(f"[bold red]❌ Error:[/bold red] {str(e)}")
            raise typer.Exit(code=1)

    def normalize_version(self, requested: str) -> str:
        """Normalizes shorthand versions to explicit semantic versions."""
        parts = requested.strip().split('.')
        if len(parts) == 1:
            return f"{requested}.0.0"
        elif len(parts) == 2:
            return f"{requested}.0"
        return requested

    def resolve(self, requested_version: str) -> str:
        """
        Validates the requested version against PyPI.
        Prompts the user if an exact match isn't found but a valid patch exists.
        """
        normalized = self.normalize_version(requested_version)
        releases = self.fetch_releases()

        # 1. Exact Match
        if normalized in releases:
            return normalized

        # 2. Match not found - search for closest valid patch release
        try:
            req_v = Version(normalized)
        except InvalidVersion:
            console.print(f"[bold red]❌ Error:[/bold red] Invalid version format: '{normalized}'")
            raise typer.Exit(code=1)

        valid_patches = []
        for r in releases:
            try:
                v = Version(r)
                # Look for releases in the same major.minor line that aren't pre-releases
                if not v.is_prerelease and v.major == req_v.major and v.minor == req_v.minor:
                    valid_patches.append(v)
            except InvalidVersion:
                continue

        if not valid_patches:
            console.print(f"[bold red]❌ Error:[/bold red] Release {normalized} not found, and no alternative patches exist for this version line.")
            raise typer.Exit(code=1)

        best_match = str(max(valid_patches))
        
        # 3. Interactive Prompt
        console.print(f"\n[yellow]Release {normalized} not found.[/yellow]")
        console.print(f"Available suggested release:\n  [bold cyan]{best_match}[/bold cyan]\n")
        
        if typer.confirm(f"Proceed with {best_match}?"):
            return best_match
        else:
            console.print("[dim]Operation cancelled by user.[/dim]")
            raise typer.Exit(code=1)