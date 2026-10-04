"""The code index, the dependency tools, blast radius and the closest names."""

import ast
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    return tmp_path / "store"


MOD = (
    "from .other import helper\n"
    "import os\n\n"
    '__all__ = ["Processor", "build"]\n\n'
    "class Processor(nn.Module):\n"
    "    def __init__(self, d_model, n_layers=4, *, dropout=0.0):\n"
    "        self.d_model = d_model\n"
    "        self.layers = []\n"
    "    def forward(self, x):\n"
    "        return x\n\n"
    "def build(cfg) -> Processor:\n"
    "    return Processor(cfg)\n\n"
    "def _private():\n"
    "    pass\n\n"
    "CONST = 5\n"
)


def _tree(root: Path):
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(MOD, encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "sub" / "inner.py").write_text("class ShouldNotIndex: pass\n", encoding="utf-8")
    (root / "venv").mkdir()
    (root / "venv" / "junk.py").write_text("x=1\n", encoding="utf-8")


def test_build_scopes_extracts_and_updates_incrementally(tmp_path):
    from windvane.code_index import _format_signature, build_code_index, extract_module

    root, idx_dir = tmp_path / "root", tmp_path / "idx"
    _tree(root)
    idx = build_code_index(str(root), idx_dir)
    assert idx is not None
    mods = idx.modules
    assert "pkg/mod.py" in mods and "sub/inner.py" not in mods and "venv/junk.py" not in mods
    m = mods["pkg/mod.py"]
    assert m["module_path"] == "pkg.mod" and m["exports"] == ["Processor", "build"]
    cls = m["classes"]["Processor"]
    assert cls["bases"] == ["nn.Module"] and cls["attrs"] == ["d_model", "layers"]
    assert cls["methods"]["__init__"] == "(self, d_model, n_layers=4, *, dropout=0.0)"
    assert cls["methods"]["forward"] == "(self, x)"
    assert m["functions"]["build"] == "(cfg) -> Processor" and "_private" in m["functions"]
    assert "os" in m["imports"] and "from .other import helper" in m["imports"]
    assert idx.resolve_symbol("Processor") == ["pkg.mod"] and idx.exports_of("pkg.mod") == ["Processor", "build"]
    fn = ast.parse("def f(a, b, /, c, *args, d, e=2, **kw) -> int: pass").body[0]
    assert _format_signature(fn.args, fn.returns) == "(a, b, /, c, *args, d, e=2, **kw) -> int"
    assert extract_module("def (:\n", "bad.py") is None

    before = dict(mods["pkg/mod.py"])
    assert build_code_index(str(root), idx_dir).modules["pkg/mod.py"] == before
    (root / "pkg" / "mod.py").unlink()
    idx3 = build_code_index(str(root), idx_dir)
    assert "pkg/mod.py" not in idx3.modules and idx3.resolve_symbol("Processor") == []


def test_reverse_edges_resolve_relative_imports(tmp_path):
    from windvane.code_index import _import_targets, build_code_index

    assert "a.b.x" in _import_targets(["from . import x"], "a.b.c", False)
    assert "a.b.d" in _import_targets(["from .d import y"], "a.b.c", False)
    assert "a.z" in _import_targets(["from .. import z"], "a.b.c", False)
    assert "a.b.x" in _import_targets(["from . import x"], "a.b", True)
    assert "pkg.mod" in _import_targets(["from pkg.mod import T"], "x.y", False)
    assert "pkg.mod" in _import_targets(["pkg.mod"], "x.y", False)

    root, idx_dir = tmp_path / "root", tmp_path / "idx"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "base.py").write_text("class Base: pass\n", encoding="utf-8")
    (root / "pkg" / "mid.py").write_text("from .base import Base\nclass Mid(Base): pass\n", encoding="utf-8")
    (root / "pkg" / "top.py").write_text("from .mid import Mid\nimport pkg.base\n", encoding="utf-8")
    idx = build_code_index(str(root), idx_dir)
    assert idx.dependents_of("pkg.base") == ["pkg.mid", "pkg.top"]
    assert idx.dependents_of("pkg.mid") == ["pkg.top"] and idx.dependents_of("pkg.top") == []
    assert (idx.module_for_file(str(root / "pkg" / "base.py")) or {}).get("module_path") == "pkg.base"
    assert idx.file_for_module("pkg.mid") == "pkg/mid.py"

    # an index written before the reverse-edge map rebuilds it on reload
    f = idx_dir / "code_index.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    del data["module_to_dependents"]
    f.write_text(json.dumps(data), encoding="utf-8")
    assert build_code_index(str(root), idx_dir).dependents_of("pkg.base") == ["pkg.mid", "pkg.top"]


def _indexed_project(tmp_path):
    """A project whose index sits where resolve_code_index looks (its store dir)."""
    from windvane.code_index import build_code_index, index_dir_for

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='p'\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "base.py").write_text(
        "class Base: pass\n\ndef parse_config_file(path):\n    pass\n\ndef load_settings():\n    pass\n", encoding="utf-8"
    )
    (root / "pkg" / "mid.py").write_text("from .base import Base\nclass Mid(Base):\n    def run(self): pass\n", encoding="utf-8")
    (root / "pkg" / "top.py").write_text("from .mid import Mid\nimport pkg.base\n", encoding="utf-8")
    build_code_index(str(root), index_dir_for(str(root)))
    return root


def test_the_index_resolves_from_a_sub_directory_and_the_import_lookups(tmp_path):
    from windvane.code_index import resolve_code_index

    root = _indexed_project(tmp_path)
    idx = resolve_code_index(str(root / "pkg"))  # walks up to the project
    assert idx is not None and idx.root() == str(root)
    assert idx.known_roots() == {"pkg"}
    assert idx.is_module("pkg.base") and idx.is_package_prefix("pkg") and not idx.is_package_prefix("pkg.typo")
    assert "Base" in idx.exports_of("pkg.base")
    assert resolve_code_index(str(tmp_path / "elsewhere")) is None
    # the miner names its store by keyword
    assert resolve_code_index(str(root), windvane_storage_dir=str(tmp_path / "store")).root() == str(root)
    assert resolve_code_index(str(root), windvane_storage_dir=str(tmp_path / "other-store")) is None


def test_blast_radius_and_impact_read_the_index(tmp_path):
    from windvane import deps

    root = _indexed_project(tmp_path)
    mod, importers = deps.blast_radius(str(root / "pkg" / "base.py"), str(root))
    assert mod == "pkg.base" and importers == ["pkg.mid", "pkg.top"]
    assert deps.blast_radius(str(root / "pkg" / "top.py"), str(root)) == ("pkg.top", [])
    assert deps.blast_radius("notes.md", str(root)) == ("", [])
    via = deps.ImpactAnalyzer()._dependents_via_index(root / "pkg" / "base.py", str(root))
    assert via is not None and "pkg/mid.py" in via and "pkg/top.py" in via
    assert deps.ImpactAnalyzer()._dependents_via_index(root / "pkg" / "top.py", str(root)) == []
    text = deps.impact_analyze(str(root / "pkg" / "base.py"), str(root), "rename Base")
    assert "Risk level:" in text and "pkg/mid.py" in text and "rename" in text


def test_deps_map_finds_a_symbols_home_and_a_files_imports(tmp_path):
    from windvane import deps

    root = _indexed_project(tmp_path)
    text = deps.deps_map(symbol="Mid", project_root=str(root))
    assert text.startswith("Symbol: Mid") and "[class(Base)]" in text and "module: pkg.mid" in text
    assert "methods: run" in text and "imported by 1: pkg.top" in text
    miss = deps.deps_map(symbol="parse_config_fil", project_root=str(root))
    assert "not in the code index" in miss and "Closest: parse_config_file" in miss
    fmap = deps.deps_map(file_path=str(root / "pkg" / "top.py"), project_root=str(root), include_reverse=True)
    assert "Found 2 imports for top.py" in fmap
    assert "needs_clarification" in deps.deps_map()
    none = deps.deps_map(symbol="X", project_root=str(tmp_path / "noindex"))
    assert none.startswith("No code index for this project yet")


def test_nearest_symbols_ranks_by_token_overlap_and_edit_distance(tmp_path):
    from windvane.code_index import nearest_symbols

    root = _indexed_project(tmp_path)
    assert nearest_symbols(str(root), "parse_config")[0] == "parse_config_file"
    assert nearest_symbols(str(root), "parseConfigFile")[0] == "parse_config_file"
    assert nearest_symbols(str(root), "def load_setings")[0] == "load_settings"
    assert nearest_symbols(str(root), "Bse", n=1) == ["Base"]
    assert len(nearest_symbols(str(root), "settings", n=2)) <= 2
    assert nearest_symbols(str(root), "zzzqqq") == []
    assert nearest_symbols(str(root), "") == []
    assert nearest_symbols(str(tmp_path / "noindex"), "Base") == []


def test_the_import_precheck_runs_on_this_index(tmp_path):
    precheck = pytest.importorskip("windvane.precheck", reason="the import precheck is the hooks port's module")
    from windvane.code_index import build_code_index

    root, idx_dir = tmp_path / "root", tmp_path / "idx"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text('__all__ = ["Processor", "build"]\nclass Processor: pass\ndef build(): pass\n', encoding="utf-8")
    idx = build_code_index(str(root), idx_dir)
    found = precheck.check_imports(idx, "from pkg.mod import Processr")
    assert len(found) == 1 and "Closest: Processor" in found[0]
    assert precheck.check_imports(idx, "from pkg.mod import Processor") == []
    assert precheck.check_imports(idx, "import pkg.typo") != []
