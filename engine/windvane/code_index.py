"""
The code index: an incremental, mtime-keyed symbol table per project.

The substrate for code awareness before an edit (import verification, blast
radius, a file's orientation, the closest names to a search that found
nothing). Parses Python with ``ast`` and records, per module: dotted path,
public exports, classes (bases / methods with signatures / attributes),
functions (with signatures) and raw imports, plus a ``symbol_to_modules``
reverse map and the reverse-import map (``module_to_dependents``).

Scope: ONE project, bounded by nested project markers. Walking a workspace
root indexes only the root's own files, not its sub-projects; each
sub-project gets its own index (resolved with workspace inheritance, like
memory). A pooled symbol table across projects would mix two services that
share a module name.

The build is incremental: a module is re-parsed only when its mtime
changes; deleted files are dropped. Pure ``ast``, no network. Degrades to
silence on any parse or read error (never records a wrong symbol).

Storage: ``<store>/projects/<hash>/code_index.json``.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Optional

# Directories never worth indexing (dependencies, caches, build output, vcs).
SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    ".idea",
    ".vscode",
}

# Files that mark a directory as its own project: the walk does not descend
# into a child directory holding one, so the index stays one project's.
PROJECT_MARKERS = {
    "pyproject.toml",
    "setup.py",
    "package.json",
    "Cargo.toml",
    "go.mod",
    ".git",
    "CLAUDE.md",
}

# Never index more than this many files in one project; the index records
# when the bound was hit (no silent truncation).
DEFAULT_MAX_FILES = 4000


# ── AST extraction ──────────────────────────────────────────────────────────


def _module_path_from_rel(rel_path: str) -> str:
    """'pkg/sub/mod.py' -> 'pkg.sub.mod'; 'pkg/__init__.py' -> 'pkg'."""
    p = rel_path.replace("\\", "/")
    if p.endswith(".py"):
        p = p[:-3]
    parts = [seg for seg in p.split("/") if seg]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _format_signature(args: ast.arguments, returns: Optional[ast.AST]) -> str:
    """A compact signature string, e.g.
    '(self, d_model, n_layers=4, *args, dropout=0.0, **kw) -> Processor'."""

    def render_default(node: Optional[ast.AST]) -> str:
        if node is None:
            return ""
        try:
            return "=" + ast.unparse(node)
        except Exception:
            return "=..."

    parts: list = []
    posonly = list(getattr(args, "posonlyargs", []) or [])
    normal = list(args.args or [])
    pos_plus_normal = posonly + normal
    defaults = list(args.defaults or [])
    n_no_default = len(pos_plus_normal) - len(defaults)
    for i, a in enumerate(pos_plus_normal):
        d = defaults[i - n_no_default] if i >= n_no_default else None
        parts.append(a.arg + render_default(d))
        if posonly and i == len(posonly) - 1:
            parts.append("/")
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for a, d in zip(args.kwonlyargs or [], args.kw_defaults or []):
        parts.append(a.arg + render_default(d))
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    sig = "(" + ", ".join(parts) + ")"
    if returns is not None:
        try:
            sig += " -> " + ast.unparse(returns)
        except Exception:
            pass
    return sig


def _import_targets(imports: list, importer: str, is_pkg: bool) -> set:
    """A module's import strings resolved to dotted module targets (absolute
    and relative), for reverse-dependency edges. Unresolved or external
    targets are kept verbatim (they just never match a real module).

    Relative resolution honours whether the importer is a package: in
    ``a/b/__init__.py`` (module ``a.b``) ``.`` is ``a.b``; in ``a/b/c.py``
    (module ``a.b.c``) ``.`` is its package ``a.b``."""
    base_parts = importer.split(".") if importer else []
    pkg_parts = base_parts if is_pkg else base_parts[:-1]
    targets: set = set()
    for imp in imports:
        if imp.startswith("from "):
            try:
                mod, names = imp[len("from "):].split(" import ", 1)
            except ValueError:
                continue
            mod = mod.strip()
            level = len(mod) - len(mod.lstrip("."))
            modpart = mod[level:].strip()
            if level:
                upto = len(pkg_parts) - (level - 1)
                root = pkg_parts[:upto] if upto >= 0 else []
                if modpart:
                    targets.add(".".join(root + [modpart]))
                else:
                    if root:
                        targets.add(".".join(root))
                    for nm in names.replace("(", " ").replace(")", " ").split(","):
                        nm = nm.split(" as ")[0].strip()
                        if nm.isidentifier():
                            targets.add(".".join(root + [nm]))
            elif modpart:
                targets.add(modpart)
        else:
            targets.add(imp.strip())
    return {t for t in targets if t}


def _base_name(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return "?"


def _class_attrs(cls: ast.ClassDef) -> list:
    """Class-level names and ``self.x`` assignments in any method."""
    attrs: set = set()
    for node in cls.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    attrs.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            attrs.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                            attrs.add(t.attr)
                elif (
                    isinstance(sub, ast.AnnAssign)
                    and isinstance(sub.target, ast.Attribute)
                    and isinstance(sub.target.value, ast.Name)
                    and sub.target.value.id == "self"
                ):
                    attrs.add(sub.target.attr)
    return sorted(attrs)


def extract_module(source: str, rel_path: str) -> Optional[dict]:
    """A module record from Python source; None on a syntax error (the
    caller keeps any prior record rather than recording garbage)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    classes: dict = {}
    functions: dict = {}
    imports: list = []
    assigned: list = []
    dunder_all: Optional[list] = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = _format_signature(node.args, node.returns)
        elif isinstance(node, ast.ClassDef):
            methods: dict = {}
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[sub.name] = _format_signature(sub.args, sub.returns)
            classes[node.name] = {
                "bases": [_base_name(b) for b in node.bases],
                "methods": methods,
                "attrs": _class_attrs(node),
            }
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {mod} import {names}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.append(t.id)
                    if t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                        dunder_all = [
                            el.value
                            for el in node.value.elts
                            if isinstance(el, ast.Constant) and isinstance(el.value, str)
                        ]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.append(node.target.id)

    if dunder_all is not None:
        exports = dunder_all
    else:
        exports = [n for n in list(classes) + list(functions) + assigned if not n.startswith("_")]
        seen: set = set()
        exports = [n for n in exports if not (n in seen or seen.add(n))]
    return {
        "module_path": _module_path_from_rel(rel_path),
        "exports": exports,
        "classes": classes,
        "functions": functions,
        "imports": imports,
    }


# ── the index ───────────────────────────────────────────────────────────────


class CodeIndex:
    """One project's symbol index: versioned, saved atomically, incremental
    by mtime, with the symbol and reverse-import maps."""

    VERSION = 1

    def __init__(self, index_path: Path):
        self._path = Path(index_path)
        self._data: dict = {
            "version": self.VERSION,
            "root": "",
            "modules": {},
            "symbol_to_modules": {},
            "truncated": False,
            "file_count": 0,
        }
        self._dirty = False
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self):
        if not self._dirty:
            return
        self._rebuild_maps()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        self._dirty = False

    @property
    def modules(self) -> dict:
        return self._data.setdefault("modules", {})

    def needs_processing(self, rel_path: str, mtime: float) -> bool:
        rec = self.modules.get(rel_path)
        return not rec or abs(rec.get("mtime", 0.0) - mtime) > 1e-6

    def update_module(self, rel_path: str, record: dict, mtime: float):
        record = dict(record)
        record["mtime"] = mtime
        self.modules[rel_path] = record
        self._dirty = True

    def drop_missing(self, present_rel_paths: set):
        gone = [r for r in self.modules if r not in present_rel_paths]
        for r in gone:
            del self.modules[r]
        if gone:
            self._dirty = True

    def _rebuild_maps(self):
        """Both derived maps from the modules table: symbol -> modules and
        module -> its importers."""
        sym: dict = {}
        deps: dict = {}
        for rel, rec in self.modules.items():
            dotted = rec.get("module_path", "")
            names = list(rec.get("classes", {})) + list(rec.get("functions", {})) + list(rec.get("exports", []))
            for n in set(names):
                sym.setdefault(n, [])
                if dotted not in sym[n]:
                    sym[n].append(dotted)
            is_pkg = rel.endswith("__init__.py")
            for tgt in _import_targets(rec.get("imports", []), dotted, is_pkg):
                if tgt != dotted:
                    deps.setdefault(tgt, set()).add(dotted)
        self._data["symbol_to_modules"] = sym
        self._data["module_to_dependents"] = {k: sorted(v) for k, v in deps.items()}

    # -- queries --
    def by_dotted(self, module_path: str) -> Optional[dict]:
        for rec in self.modules.values():
            if rec.get("module_path") == module_path:
                return rec
        return None

    def exports_of(self, module_path: str) -> Optional[list]:
        rec = self.by_dotted(module_path)
        return rec.get("exports") if rec else None

    def resolve_symbol(self, name: str) -> list:
        return self._data.get("symbol_to_modules", {}).get(name, [])

    def module_paths(self) -> set:
        return {rec.get("module_path", "") for rec in self.modules.values()}

    def is_module(self, dotted: str) -> bool:
        return dotted in self.module_paths()

    def is_package_prefix(self, dotted: str) -> bool:
        """True if ``dotted`` names a package (a module, or the prefix of
        one); covers namespace packages with no ``__init__`` record."""
        if not dotted:
            return False
        pref = dotted + "."
        return any(mp == dotted or mp.startswith(pref) for mp in self.module_paths())

    def known_roots(self) -> set:
        """Top-level package names this index covers: an import under one of
        them is internal (verifiable); anything else is left alone."""
        return {mp.split(".")[0] for mp in self.module_paths() if mp}

    def dependents_of(self, module_path: str) -> list:
        """Modules that import ``module_path`` (the blast radius)."""
        return self._data.get("module_to_dependents", {}).get(module_path, [])

    def root(self) -> str:
        return self._data.get("root", "")

    def module_for_file(self, file_path: str) -> Optional[dict]:
        """The module record for a file path, matched against the indexed root."""
        root = self._data.get("root", "")
        if not root:
            return None
        try:
            rel = Path(file_path).resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            return None
        return self.modules.get(rel)

    def file_for_module(self, module_path: str) -> Optional[str]:
        for rel, rec in self.modules.items():
            if rec.get("module_path") == module_path:
                return rel
        return None

    def all_symbols(self) -> list:
        return list(self._data.get("symbol_to_modules", {}))

    def module_count(self) -> int:
        return len(self.modules)

    def symbol_count(self) -> int:
        return len(self._data.get("symbol_to_modules", {}))

    @property
    def truncated(self) -> bool:
        return bool(self._data.get("truncated"))


# ── build and resolve ───────────────────────────────────────────────────────


def _iter_python_files(root: Path, max_files: int) -> tuple:
    """.py files under ``root``, skipping SKIP_DIRS, hidden dirs and nested
    project-marker dirs. Returns (files, truncated)."""
    files: list = []
    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if dirpath != root_str:
            here = Path(dirpath)
            if any((here / m).exists() for m in PROJECT_MARKERS):
                dirnames[:] = []
                continue
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(Path(dirpath) / fn)
                if len(files) >= max_files:
                    return files, True
    return files, False


def build_code_index(project_root: str, index_dir: Path, max_files: int = DEFAULT_MAX_FILES) -> Optional[CodeIndex]:
    """Build or update the index of the project at ``project_root``, stored
    at ``index_dir/code_index.json``. Re-parses only files whose mtime
    changed; drops deleted files."""
    root = Path(project_root)
    if not root.is_dir():
        return None
    index = CodeIndex(Path(index_dir) / "code_index.json")
    index._data["root"] = str(root)
    # An index written before the reverse-edge map: rebuild the derived maps
    # on save (from the modules table, no re-parse).
    if "module_to_dependents" not in index._data:
        index._dirty = True
    files, truncated = _iter_python_files(root, max_files)
    present: set = set()
    for fp in files:
        try:
            rel = fp.relative_to(root).as_posix()
        except ValueError:
            rel = fp.as_posix()
        present.add(rel)
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            continue
        if not index.needs_processing(rel, mtime):
            continue
        try:
            source = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        record = extract_module(source, rel)
        if record is not None:
            index.update_module(rel, record, mtime)
    index.drop_missing(present)
    if index._data.get("truncated") != truncated:
        index._data["truncated"] = truncated
        index._dirty = True
    index._data["file_count"] = len(present)
    index.save()
    return index


def _memory_dir(project_path: str, windvane_storage_dir: str = "") -> Optional[Path]:
    """The store directory for a project (registered or not)."""
    try:
        from windvane.store import project_store_dir

        return project_store_dir(project_path, windvane_storage_dir)
    except Exception:
        return None


def index_dir_for(project_path: str, windvane_storage_dir: str = "") -> Optional[Path]:
    """Where a project's index lives (its store directory)."""
    return _memory_dir(project_path, windvane_storage_dir)


def index_paths(project_path: str, windvane_storage_dir: str = ""):
    """The index files that exist for a project and its ancestors, nearest
    first (nothing is loaded)."""
    p = Path(project_path)
    seen: set = set()
    while True:
        d = _memory_dir(str(p), windvane_storage_dir)
        if d is not None and str(d) not in seen:
            seen.add(str(d))
            idx_path = d / "code_index.json"
            if idx_path.exists():
                yield idx_path
        parent = p.parent
        if parent == p:
            break
        p = parent


def resolve_code_index(project_path: str, windvane_storage_dir: str = "") -> Optional[CodeIndex]:
    """The existing index for a project (no build), walking up to a parent
    project when the sub-project has none yet: workspace inheritance, as
    memory and checkpoints resolve. ``windvane_storage_dir`` names the store
    (default: the configured one)."""
    for idx_path in index_paths(project_path, windvane_storage_dir):
        idx = CodeIndex(idx_path)
        if idx.module_count() > 0:
            return idx
    return None


# ── the closest names ───────────────────────────────────────────────────────

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[A-Z]+")
_QUERY_NOISE = frozenset({"def", "class", "import", "from", "async", "self", "return", "lambda"})


def _name_tokens(name: str) -> set:
    """``parseConfigFile`` / ``parse_config_file`` -> {parse, config, file}."""
    out: set = set()
    for part in name.split("_"):
        for t in _CAMEL.findall(part):
            if t:
                out.add(t.lower())
    return out


def _edit_similarity(a: str, b: str) -> float:
    """1 - Levenshtein(a, b) / max(len), case-insensitive."""
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))


def nearest_symbols(project: str, query: str, n: int = 5, index: Optional[CodeIndex] = None) -> list:
    """The ``n`` symbol names in the project's code index closest to
    ``query`` (a name, or a search pattern that found nothing), best first:
    half token overlap (snake_case and camelCase split into words), half
    edit-distance similarity to the query's longest identifier. Names that
    share no token and are less than half alike are left out. [] when the
    project has no index or the query names nothing."""
    idents = [w for w in _IDENT.findall(str(query or "")) if w.lower() not in _QUERY_NOISE]
    if not idents or n <= 0:
        return []
    idx = index if index is not None else resolve_code_index(project)
    if idx is None:
        return []
    target = max(idents, key=len)
    q_tokens: set = set()
    for w in idents:
        q_tokens |= _name_tokens(w)
    scored = []
    for name in idx.all_symbols():
        tokens = _name_tokens(name)
        union = q_tokens | tokens
        overlap = len(q_tokens & tokens) / len(union) if union else 0.0
        edit = _edit_similarity(target, name)
        if overlap == 0.0 and edit < 0.5:
            continue
        scored.append((0.5 * overlap + 0.5 * edit, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _s, name in scored[:n]]
