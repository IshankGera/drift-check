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
    return sorted((Path(c.file_path).name, c.method_name) for c in calls)


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
    assert analyze(tmp_path, {"a.py": "import mock_sdk\nmock_sdk.Client()\n"}) == []


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
    (call,) = [c for c in calls if c.package_name == "openai"]
    assert call.literal_kwargs == {"model": "o3-mini", "max_tokens": 100}
    assert call.kwargs_passed == ["model", "max_tokens", "temperature"]
    assert call.has_dynamic_kwargs is True
