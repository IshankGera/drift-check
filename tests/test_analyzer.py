"""Call-site tracing: the patterns real projects use."""
import textwrap
from pathlib import Path

from drift_check.analyzer import ASTAnalyzer


def analyze(tmp_path, files):
    paths = []
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src))
        paths.append(p)
    return ASTAnalyzer(paths, root=tmp_path).analyze()


def methods(calls):
    return sorted((Path(c.file_path).name, c.method_name) for c in calls if c.kind == "call")


def imports(calls):
    return sorted((c.package_name, c.method_name, c.line_number) for c in calls if c.kind == "import")


def test_module_function_and_same_file_client(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        import openai
        from openai import OpenAI
        openai.ChatCompletion.create(model="x")
        c = OpenAI()
        c.chat.completions.create(model="x")
    """})
    assert methods(calls) == [("a.py", "ChatCompletion.create"), ("a.py", "OpenAI.chat.completions.create")]


def test_self_client_inside_class(tmp_path):
    calls = analyze(tmp_path, {"svc.py": """
        from openai import OpenAI
        class LLM:
            def __init__(self):
                self.client = OpenAI()
            def ask(self):
                return self.client.chat.completions.create(model="x")
    """})
    assert methods(calls) == [("svc.py", "OpenAI.chat.completions.create")]


def test_client_imported_from_another_module(tmp_path):
    calls = analyze(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/llm.py": "import openai\nclient = openai.OpenAI()\n",
        "app.py": "from pkg.llm import client\nclient.chat.completions.create(model='x')\n",
        "pkg/rel.py": "from .llm import client as c\nc.embeddings.create(model='e')\n",
    })
    assert ("app.py", "OpenAI.chat.completions.create") in methods(calls)
    assert ("rel.py", "OpenAI.embeddings.create") in methods(calls)


def test_chained_constructor(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        from openai import OpenAI
        OpenAI(api_key="k").chat.completions.create(model="x")
    """})
    assert methods(calls) == [("a.py", "OpenAI.chat.completions.create")]


def test_constructor_itself_is_not_a_call_site(tmp_path):
    assert methods(analyze(tmp_path, {"a.py": "import mock_sdk\nmock_sdk.Client()\n"})) == []


def test_unrelated_self_attributes_are_ignored(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        class A:
            def run(self):
                self.helper.create(x=1)
    """})
    assert calls == []


def test_literal_kwargs_are_recorded(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        from openai import OpenAI
        import os
        c = OpenAI()
        c.chat.completions.create(model="o3-mini", max_tokens=100, temperature=os.getenv("T"), **{})
    """})
    (call,) = [c for c in calls if c.package_name == "openai" and c.kind == "call"]
    assert call.literal_kwargs == {"model": "o3-mini", "max_tokens": 100}
    assert call.kwargs_passed == ["model", "max_tokens", "temperature"]
    assert call.has_dynamic_kwargs is True


# ---------- import-aware tracking ----------

def test_from_imported_function_call(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        from mock_sdk import old_method, create as make
        old_method()
        make(name="t", old_param=1)
    """})
    assert methods(calls) == [("a.py", "create"), ("a.py", "old_method")]
    make_call = next(c for c in calls if c.kind == "call" and c.method_name == "create")
    assert make_call.kwargs_passed == ["name", "old_param"]


def test_submodule_paths(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        import pkg.sub as s
        from pkg.sub.deep import helper
        from pkg.lib.azure import AzureClient
        s.fn(x=1)
        helper()
        c = AzureClient()
        c.chat.create()
    """})
    assert methods(calls) == [
        ("a.py", "lib.azure.AzureClient.chat.create"), ("a.py", "sub.deep.helper"), ("a.py", "sub.fn"),
    ]


def test_import_sites_are_recorded(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        import openai
        import openai.types
        from openai import OpenAI, ChatCompletion
        from openai.lib import azure as az
        from . import local_thing
        from os import *
    """})
    assert imports(calls) == [
        ("openai", "ChatCompletion", 4), ("openai", "OpenAI", 4),
        ("openai", "lib.azure", 5), ("openai", "types", 3),
    ]


def test_plain_name_call_without_import_is_ignored(tmp_path):
    assert methods(analyze(tmp_path, {"a.py": "def f():\n    pass\nf()\n"})) == []


def test_constructors_and_argument_counts(tmp_path):
    calls = analyze(tmp_path, {"a.py": """
        import sdk
        from sdk import Client, fn
        Client("key", base_url="x")
        sdk.Other()
        fn(1, 2, *rest)
    """})
    shape = sorted((c.method_name, c.kind, c.positional_count, c.has_star_args) for c in calls if c.kind != "import")
    assert shape == [("Client", "constructor", 1, False), ("Other", "constructor", 0, False), ("fn", "call", 2, True)]
