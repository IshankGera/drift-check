"""Baseline declarations from requirements.txt, pyproject.toml and uv.lock."""
import textwrap

from drift_check.dependencies import DependencyDeclarationResolver, canonicalize


def project(tmp_path, **files):
    for name, content in files.items():
        (tmp_path / name.replace("__", ".")).write_text(textwrap.dedent(content))
    return DependencyDeclarationResolver(str(tmp_path))


def test_requirements_txt(tmp_path):
    r = project(tmp_path, requirements__txt="""
        # comment
        -r other.txt
        OpenAI[datalib]>=1.40  # pinned for prod
        zope.interface==6.0
    """)
    assert r.find("openai") == "OpenAI[datalib]>=1.40"
    assert r.find("zope-interface") == "zope.interface==6.0"
    assert r.find("missing") is None


def test_pyproject_dependencies_groups_and_extras(tmp_path):
    r = project(tmp_path, pyproject__toml="""
        [project]
        name = "my-app"
        dependencies = ["openai>=1.40,<2", "my-app[extra]"]

        [project.optional-dependencies]
        data = ["pydantic==1.10.13"]

        [dependency-groups]
        dev = ["pytest>=8", {include-group = "lint"}]
        lint = ["ruff"]
    """)
    assert r.find("openai") == "openai>=1.40,<2"
    assert r.find("pydantic") == "pydantic==1.10.13"
    assert r.find("pytest") == "pytest>=8"
    assert r.find("my-app") is None  # the project itself is not a dependency


def test_uv_lock_pins_exact_version(tmp_path):
    r = project(tmp_path, uv__lock="""
        version = 1

        [[package]]
        name = "my-app"
        version = "0.1.0"
        source = { editable = "." }

        [[package]]
        name = "openai"
        version = "1.40.0"
        source = { registry = "https://pypi.org/simple" }
    """)
    assert r.find("openai") == "openai==1.40.0"
    assert r.find("my-app") is None


def test_requirements_win_over_pyproject(tmp_path):
    r = project(tmp_path,
                requirements__txt="openai==1.40.0\n",
                pyproject__toml='[project]\nname = "x"\ndependencies = ["openai>=1"]\n')
    assert r.find("openai") == "openai==1.40.0"


def test_broken_toml_is_ignored(tmp_path):
    assert project(tmp_path, pyproject__toml="[project\n").find("openai") is None


def test_canonicalize():
    assert canonicalize("Zope_Interface.x") == "zope-interface-x"
