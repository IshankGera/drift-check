import json
import subprocess
import sys
import textwrap

import pytest

from drift_check.signatures import INLINE_WORKER

FAKE_SDK = {
    "fakesdk/__init__.py": """
        from functools import cached_property
        from typing import Optional

        def top_level(name, timeout=None):
            pass

        def dynamic(**kwargs):
            pass

        class Completions:
            def create(self, model, messages, temperature=None):
                pass

        class Chat:
            @cached_property
            def completions(self) -> Completions:
                return Completions()

        def _factory():
            return Completions()

        class Untyped:
            def __init__(self):
                self.thing = _factory()  # return type not inferable from the AST

            def go(self, fast=True):
                pass

        class Client:
            def __init__(self):
                self.untyped = Untyped()
                self.typed: Completions = Completions()

            @cached_property
            def chat(self) -> Chat:
                return Chat()

            @property
            def maybe(self) -> Optional[Chat]:
                return None

            @property
            def mystery(self):
                return Chat()

            def ping(self, verbose=False):
                pass

            @classmethod
            def from_env(cls, prefix="X"):
                pass
    """,
    "brokensdk/__init__.py": "raise ImportError('missing optional dependency')\n",
}


@pytest.fixture(scope="session")
def sdk_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("sdk")
    for rel, src in FAKE_SDK.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(src))
    return root


@pytest.fixture
def worker(sdk_dir):
    def run(package, method):
        out = subprocess.run(
            [sys.executable, "-c", INLINE_WORKER, package, method],
            capture_output=True, text=True, cwd=sdk_dir,
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])
    return run
