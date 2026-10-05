"""
Dependencies: where a symbol lives, what a file imports, what imports it,
and what an edit to it may break.

- ``deps_map(symbol=...)``: "where is X defined?" from the code index:
  file, kind, signature, importers. Cheaper than a grep and a read.
- ``deps_map(file_path=...)``: a file's imports (stdlib / external /
  internal) and, with ``include_reverse``, the files that import it.
- ``impact_analyze``: a file's exported symbols, its dependents (from the
  index's reverse edges, else a scan), where the symbols are used, and a
  risk level.
- ``blast_radius``: the modules that import a file, from the index alone.

Read-only; pure ``ast``/regex, no network.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from windvane.store import Response, WorkLog

_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
_SEARCH_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java"}
_PY_STDLIB = {
    "os", "sys", "re", "json", "time", "datetime", "collections", "itertools", "functools", "pathlib",
    "typing", "asyncio", "subprocess", "threading", "multiprocessing", "logging", "unittest", "pytest",
    "argparse", "dataclasses", "enum", "abc", "io", "hashlib", "random", "math", "copy", "pickle",
}


# ---------------------------------------------------------------------------
# Symbol lookup and blast radius (the code index)
# ---------------------------------------------------------------------------


def symbol_lookup(symbol: str, project_root: str, index=None) -> str:
    """Where ``symbol`` is defined, from the project's code index: the
    defining module(s), file, kind, signature, and who imports it. When the
    name is not indexed, the closest names."""
    import difflib

    from windvane.code_index import resolve_code_index

    idx = index if index is not None else resolve_code_index(project_root)
    if idx is None:
        return "No code index for this project yet - it builds during background mining."
    mods = idx.resolve_symbol(symbol)
    if not mods:
        close = difflib.get_close_matches(symbol, idx.all_symbols(), n=3, cutoff=0.75)
        hint = f" Closest: {', '.join(close)}." if close else ""
        return f"Symbol '{symbol}' not in the code index ({idx.module_count()} modules indexed).{hint}"
    root = idx.root()
    lines = [f"Symbol: {symbol}"]
    for dotted in mods[:5]:
        rec = idx.by_dotted(dotted) or {}
        rel = idx.file_for_module(dotted) or ""
        loc = f"{root}/{rel}" if root and rel else (rel or dotted)
        kind, sig_lines = "export", []
        if symbol in rec.get("classes", {}):
            c = rec["classes"][symbol]
            kind = f"class({', '.join(c.get('bases', [])) or 'object'})"
            methods = c.get("methods", {})
            if "__init__" in methods:
                sig_lines.append(f"__init__{methods['__init__']}")
            names = [m for m in methods if m != "__init__"]
            if names:
                more = f" (+{len(names) - 10})" if len(names) > 10 else ""
                sig_lines.append(f"methods: {', '.join(names[:10])}{more}")
        elif symbol in rec.get("functions", {}):
            kind = "function"
            sig_lines.append(f"def {symbol}{rec['functions'][symbol]}")
        lines.append(f"\n{loc}  [{kind}]  module: {dotted}")
        for s in sig_lines:
            lines.append(f"  {s}")
        importers = idx.dependents_of(dotted)
        if importers:
            more = f" (+{len(importers) - 8} more)" if len(importers) > 8 else ""
            lines.append(f"  imported by {len(importers)}: {', '.join(importers[:8])}{more}")
        else:
            lines.append("  imported by: nothing in the index")
    if len(mods) > 5:
        lines.append(f"\n...also defined in {len(mods) - 5} more module(s)")
    return "\n".join(lines)


def blast_radius(file_path: str, project_path: str = "") -> tuple:
    """(module path, the modules that import it) for a Python file, from the
    index's reverse edges. ("", []) when the file is not indexed."""
    if not str(file_path).endswith(".py"):
        return "", []
    try:
        from windvane.code_index import resolve_code_index

        idx = resolve_code_index(project_path or str(Path(file_path).parent))
        if idx is None:
            return "", []
        rec = idx.module_for_file(file_path)
        if not rec:
            return "", []
        mod = rec.get("module_path", "")
        return mod, list(idx.dependents_of(mod))
    except Exception:
        return "", []


# ---------------------------------------------------------------------------
# A file's imports and its reverse dependencies
# ---------------------------------------------------------------------------


class DependencyMapper:
    """What a file imports, and (optionally) what imports it."""

    def map_file(self, file_path: str, project_root: Optional[str] = None, include_reverse: bool = False) -> Response:
        work_log = WorkLog()
        work_log.what_i_tried.append("dependency extraction")
        path = Path(file_path)
        if not path.exists():
            return Response(status="failed", confidence="high", reasoning=f"File does not exist: {file_path}", work_log=work_log)
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            work_log.files_examined = 1
        except Exception as e:
            return Response(status="failed", confidence="high", reasoning=f"Could not read file: {e}", work_log=work_log)
        ext = path.suffix.lower()
        imports = self._extract_imports(content, ext)
        work_log.what_worked.append(f"found {len(imports)} imports")
        categorized = self._categorize_imports(imports, ext)
        data: dict = {
            "file": str(path),
            "imports": {
                "all": imports,
                "stdlib": categorized.get("stdlib", []),
                "external": categorized.get("external", []),
                "internal": categorized.get("internal", []),
            },
        }
        if include_reverse and project_root:
            work_log.what_i_tried.append("reverse dependency search")
            reverse_deps = self._find_reverse_deps(path, project_root, ext)
            data["imported_by"] = reverse_deps
            work_log.files_examined += reverse_deps.get("files_scanned", 0)
            work_log.what_worked.append(f"found {len(reverse_deps.get('files', []))} reverse deps")
        return Response(
            status="success",
            confidence="high" if imports else "medium",
            reasoning=f"Found {len(imports)} imports for {path.name}",
            work_log=work_log,
            data=data,
            suggestions=self._generate_suggestions(data),
        )

    def _extract_imports(self, content: str, extension: str) -> list:
        imports: list = []
        if extension == ".py":
            imports.extend(re.findall(r"^from\s+([\w.]+)\s+import", content, re.MULTILINE))
            imports.extend(re.findall(r"^import\s+([\w.]+)", content, re.MULTILINE))
        elif extension in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
            imports.extend(re.findall(r"import\s+.*?from\s+['\"]([^'\"]+)['\"]", content))
            imports.extend(re.findall(r"require\(['\"]([^'\"]+)['\"]\)", content))
        elif extension == ".go":
            imports.extend(re.findall(r'import\s+["\(]([^"\)]+)', content))
            imports.extend(re.findall(r'"([^"]+)"', content[:3000]))
        elif extension == ".rs":
            imports.extend(re.findall(r"use\s+([\w:]+)", content))
        elif extension == ".java":
            imports.extend(re.findall(r"import\s+([\w.]+);", content))
        elif extension in (".c", ".cpp", ".h", ".hpp"):
            imports.extend(re.findall(r'#include\s*[<"]([^>"]+)[>"]', content))
        return sorted(set(imports))

    def _categorize_imports(self, imports: list, extension: str) -> dict:
        categorized: dict = {"stdlib": [], "external": [], "internal": []}
        for imp in imports:
            if extension == ".py":
                if imp.split(".")[0] in _PY_STDLIB:
                    categorized["stdlib"].append(imp)
                elif imp.startswith("."):
                    categorized["internal"].append(imp)
                else:
                    categorized["external"].append(imp)
            elif extension in (".js", ".ts", ".jsx", ".tsx"):
                if imp.startswith(".") or imp.startswith("/"):
                    categorized["internal"].append(imp)
                else:
                    categorized["external"].append(imp)
            else:
                categorized["external"].append(imp)
        return categorized

    def _find_reverse_deps(self, target_path: Path, project_root: str, extension: str) -> dict:
        """Files that import the target (a heuristic scan)."""
        result: dict = {"files": [], "files_scanned": 0}
        root = Path(project_root)
        target_name = target_path.stem
        for root_dir, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for filename in files:
                filepath = Path(root_dir) / filename
                if filepath.suffix.lower() not in _SEARCH_EXTENSIONS or filepath == target_path:
                    continue
                result["files_scanned"] += 1
                try:
                    content = filepath.read_text(encoding="utf-8", errors="ignore")
                    if target_name not in content:
                        continue
                    if _imports_target(content, target_name, extension):
                        result["files"].append(str(filepath.relative_to(root)))
                except Exception:
                    continue
        return result

    def _generate_suggestions(self, data: dict) -> list:
        suggestions = []
        imports = data.get("imports", {})
        external = imports.get("external", [])
        if len(external) > 10:
            suggestions.append(f"This file has {len(external)} external dependencies - consider if all are needed")
        if imports.get("internal", []):
            suggestions.append("Check for circular dependencies in internal imports")
        imported_by = data.get("imported_by", {})
        if imported_by.get("files") and len(imported_by["files"]) > 5:
            suggestions.append(
                f"This file is imported by {len(imported_by['files'])} other files - changes may have wide impact"
            )
        return suggestions


def _imports_target(content: str, target_name: str, ext: str) -> bool:
    """Does ``content`` import the module named ``target_name``?"""
    if target_name not in content:
        return False
    if ext == ".py":
        patterns = [
            rf"from\s+[\w.]*{re.escape(target_name)}\s+import",
            rf"import\s+[\w.]*{re.escape(target_name)}",
        ]
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        patterns = [
            rf"from\s+['\"].*{re.escape(target_name)}['\"]",
            rf"require\(['\"].*{re.escape(target_name)}['\"]\)",
        ]
    else:
        patterns = [rf"\b{re.escape(target_name)}\b"]
    return any(re.search(p, content) for p in patterns)


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------


@dataclass
class ExportedSymbol:
    name: str
    kind: str  # "function", "class", "constant", "variable"
    line: int
    is_public: bool = True


@dataclass
class SymbolUsage:
    file: str
    line: int
    context: str


@dataclass
class ImpactReport:
    file: str
    dependents: list = field(default_factory=list)
    exported_symbols: list = field(default_factory=list)
    symbol_usages: dict = field(default_factory=dict)
    risk_level: str = "low"  # "low", "medium", "high", "critical"
    risk_reasons: list = field(default_factory=list)


class ImpactAnalyzer:
    """What an edit to a file may break: its exports, its dependents, where
    the exports are used, and a risk level."""

    def analyze(self, file_path: str, project_root: str, proposed_changes: Optional[str] = None) -> Response:
        work_log = WorkLog()
        work_log.what_i_tried.append("impact analysis")
        path = Path(file_path)
        root = Path(project_root)
        if not path.exists():
            return self._error_response(f"File does not exist: {file_path}", work_log)
        if not root.exists():
            return self._error_response(f"Project root does not exist: {project_root}", work_log)
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return self._error_response(f"Could not read file: {e}", work_log)
        ext = path.suffix.lower()
        report = ImpactReport(file=str(path))
        work_log.what_i_tried.append("extract exports")
        report.exported_symbols = self._extract_exports(content, ext)
        work_log.what_worked.append(f"found {len(report.exported_symbols)} exports")
        # The index's reverse edges first (ast-accurate, no walk); the scan
        # when the index does not know the file.
        work_log.what_i_tried.append("find dependents")
        idx_deps = self._dependents_via_index(path, project_root)
        report.dependents = idx_deps if idx_deps is not None else self._find_dependents(path, root, ext)
        work_log.files_examined = len(report.dependents) + 1
        work_log.what_worked.append(f"found {len(report.dependents)} dependent files")
        if report.exported_symbols and report.dependents:
            work_log.what_i_tried.append("track symbol usages")
            report.symbol_usages = self._track_symbol_usages(report.exported_symbols, report.dependents, root)
            usage_count = sum(len(u) for u in report.symbol_usages.values())
            work_log.what_worked.append(f"tracked {usage_count} symbol usages")
        report.risk_level, report.risk_reasons = self._assess_risk(report, proposed_changes)
        return self._build_response(report, work_log)

    def _extract_exports(self, content: str, ext: str) -> list:
        if ext == ".py":
            return self._extract_python_exports(content)
        if ext in (".js", ".ts", ".jsx", ".tsx"):
            return self._extract_js_exports(content)
        if ext == ".go":
            return self._extract_go_exports(content)
        return []

    def _extract_python_exports(self, content: str) -> list:
        exports = []
        for i, line in enumerate(content.split("\n"), 1):
            m = re.match(r"^def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", line)
            if m:
                exports.append(ExportedSymbol(m.group(1), "function", i, not m.group(1).startswith("_")))
            m = re.match(r"^class\s+([a-zA-Z_][a-zA-Z0-9_]*)", line)
            if m:
                exports.append(ExportedSymbol(m.group(1), "class", i, not m.group(1).startswith("_")))
            m = re.match(r"^([A-Z][A-Z0-9_]+)\s*=", line)
            if m:
                exports.append(ExportedSymbol(m.group(1), "constant", i, True))
        return exports

    def _extract_js_exports(self, content: str) -> list:
        exports = []
        for m in re.finditer(
            r"export\s+(default\s+)?(function|const|let|class|async function)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)", content
        ):
            kind = m.group(2)
            kind = "constant" if kind in ("const", "let") else ("function" if kind == "async function" else kind)
            exports.append(ExportedSymbol(m.group(3), kind, content[: m.start()].count("\n") + 1, True))
        for m in re.finditer(r"export\s*\{([^}]+)\}", content):
            line = content[: m.start()].count("\n") + 1
            for name in (n.strip().split(" as ")[0].strip() for n in m.group(1).split(",")):
                if name:
                    exports.append(ExportedSymbol(name, "variable", line, True))
        return exports

    def _extract_go_exports(self, content: str) -> list:
        exports = []
        for m in re.finditer(r"^func\s+(?:\([^)]+\)\s+)?([A-Z][a-zA-Z0-9_]*)\s*\(", content, re.MULTILINE):
            exports.append(ExportedSymbol(m.group(1), "function", content[: m.start()].count("\n") + 1, True))
        for m in re.finditer(r"^type\s+([A-Z][a-zA-Z0-9_]*)\s+", content, re.MULTILINE):
            exports.append(ExportedSymbol(m.group(1), "class", content[: m.start()].count("\n") + 1, True))
        return exports

    def _dependents_via_index(self, path: Path, project_root: str):
        """Dependents from the code index, or None when the file is not
        indexed (the caller scans). An empty list is authoritative."""
        try:
            from windvane.code_index import resolve_code_index

            idx = resolve_code_index(project_root)
            if idx is None:
                return None
            rec = idx.module_for_file(str(path))
            if not rec:
                return None
            files = []
            for dotted in idx.dependents_of(rec.get("module_path", "")):
                rel = idx.file_for_module(dotted)
                if rel:
                    files.append(rel)
            return files
        except Exception:
            return None

    def _find_dependents(self, target: Path, root: Path, ext: str) -> list:
        dependents = []
        for root_dir, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for filename in files:
                filepath = Path(root_dir) / filename
                if filepath.suffix.lower() not in _SEARCH_EXTENSIONS or filepath == target:
                    continue
                try:
                    if _imports_target(filepath.read_text(encoding="utf-8", errors="ignore"), target.stem, ext):
                        dependents.append(str(filepath.relative_to(root)))
                except Exception:
                    continue
        return dependents

    def _track_symbol_usages(self, symbols: list, dependents: list, root: Path) -> dict:
        usages: dict = {}
        public_symbols = [s for s in symbols if s.is_public]
        for dep_path in dependents:
            try:
                lines = (root / dep_path).read_text(encoding="utf-8", errors="ignore").split("\n")
                for symbol in public_symbols:
                    pattern = rf"\b{re.escape(symbol.name)}\b"
                    for i, line in enumerate(lines, 1):
                        if re.search(pattern, line):
                            usages.setdefault(symbol.name, []).append(SymbolUsage(dep_path, i, line.strip()[:100]))
            except Exception:
                continue
        return usages

    def _assess_risk(self, report: ImpactReport, proposed_changes: Optional[str]) -> tuple:
        reasons = []
        score = 0
        dep_count = len(report.dependents)
        if dep_count == 0:
            reasons.append("No files depend on this - changes are isolated")
        elif dep_count <= 2:
            score += 1
            reasons.append(f"{dep_count} file(s) depend on this")
        elif dep_count <= 5:
            score += 2
            reasons.append(f"{dep_count} files depend on this - moderate reach")
        else:
            score += 3
            reasons.append(f"{dep_count} files depend on this - wide impact")
        export_count = len([s for s in report.exported_symbols if s.is_public])
        if export_count > 10:
            score += 1
            reasons.append(f"File exports {export_count} public symbols")
        total_usages = sum(len(u) for u in report.symbol_usages.values())
        if total_usages > 20:
            score += 2
            reasons.append(f"Symbols are used {total_usages} times across dependents")
        elif total_usages > 5:
            score += 1
            reasons.append(f"Symbols are used {total_usages} times")
        if proposed_changes:
            for word in ("rename", "delete", "remove", "signature", "parameter", "return type"):
                if word in proposed_changes.lower():
                    score += 1
                    reasons.append(f"Proposed change involves '{word}' - may break callers")
                    break
        if score == 0:
            level = "low"
        elif score <= 2:
            level = "medium"
        elif score <= 4:
            level = "high"
        else:
            level = "critical"
        return level, reasons

    def _build_response(self, report: ImpactReport, work_log: WorkLog) -> Response:
        # Plain strings render one per line; dicts would print raw.
        exports_data = [
            f"{s.name} ({s.kind}, line {s.line}, {'public' if s.is_public else 'private'})" for s in report.exported_symbols
        ]
        usages_data = []
        for symbol, usages in report.symbol_usages.items():
            locs = ", ".join(f"{u.file}:{u.line}" for u in usages[:5])
            more = "" if len(usages) <= 5 else f" (+{len(usages) - 5} more)"
            usages_data.append(f"{symbol} - {locs}{more}")
        data = {
            "file": report.file,
            "risk_level": report.risk_level,
            "risk_reasons": report.risk_reasons,
            "dependents": report.dependents,
            "exports": exports_data,
            "symbol_usages": usages_data,
            "summary": {
                "dependent_count": len(report.dependents),
                "export_count": len(report.exported_symbols),
                "public_export_count": len([s for s in report.exported_symbols if s.is_public]),
                "total_usages": sum(len(u) for u in report.symbol_usages.values()),
            },
        }
        warnings = []
        if report.risk_level in ("high", "critical"):
            warnings.append(f"Risk level is {report.risk_level.upper()} - consider the impact carefully")
        return Response(
            status="success",
            confidence="high",
            reasoning=f"Impact analysis complete. Risk level: {report.risk_level}",
            work_log=work_log,
            data=data,
            suggestions=self._generate_suggestions(report),
            warnings=warnings,
        )

    def _generate_suggestions(self, report: ImpactReport) -> list:
        suggestions = []
        if report.dependents:
            suggestions.append(f"Review these files before changing: {', '.join(report.dependents[:3])}")
        if report.risk_level in ("high", "critical"):
            suggestions.append("Consider adding tests for dependent code before making changes")
            suggestions.append("Make changes incrementally and test after each step")
        if report.symbol_usages:
            for name, usages in sorted(report.symbol_usages.items(), key=lambda x: len(x[1]), reverse=True)[:3]:
                if len(usages) > 2:
                    suggestions.append(f"'{name}' is used {len(usages)} times - changes will have wide effect")
        return suggestions

    def _error_response(self, message: str, work_log: WorkLog) -> Response:
        return Response(status="failed", confidence="high", reasoning=message, work_log=work_log)


# ---------------------------------------------------------------------------
# The tool operations
# ---------------------------------------------------------------------------


def deps_map(
    file_path: str = "", project_root: str = "", include_reverse: bool = False, symbol: str = "", index=None
) -> str:
    """A symbol's home (``symbol``), or a file's dependency map (``file_path``).
    ``index`` is a code index already loaded for the project (a warm caller's)."""
    if symbol:
        base = project_root or (str(Path(file_path).parent) if file_path else "") or os.getcwd()
        return symbol_lookup(symbol, base, index)
    if not file_path:
        return Response(
            status="needs_clarification",
            confidence="high",
            reasoning="No file path or symbol provided",
            questions=["Which file should I analyze dependencies for (file_path), or which symbol should I locate (symbol)?"],
        ).to_formatted_string()
    return DependencyMapper().map_file(file_path, project_root or None, include_reverse).to_formatted_string()


def impact_analyze(file_path: str, project_root: str, proposed_changes: Optional[str] = None) -> str:
    """What an edit to ``file_path`` may break."""
    if not file_path:
        return Response(
            status="needs_clarification",
            confidence="high",
            reasoning="No file path provided",
            questions=["Which file do you want to analyze for change impact?"],
        ).to_formatted_string()
    if not project_root:
        return Response(
            status="needs_clarification",
            confidence="high",
            reasoning="No project root provided",
            questions=["What is the project root directory?"],
        ).to_formatted_string()
    return ImpactAnalyzer().analyze(file_path, project_root, proposed_changes).to_formatted_string()
