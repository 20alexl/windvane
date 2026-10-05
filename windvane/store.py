"""
The memory store: what windvane remembers about a project.

Per-project entries (rules, mistakes, decisions, discoveries, context notes)
live in ``<store>/projects/<hash>/memory.json``, registered in
``<store>/manifest.json``. This module holds the entry model, project
registration, the hot tier (load, save, the freshness guard), the writers
behind ``memory(remember)``, search / recent / modify / delete / promote, the
injection scoring and the optional embedding paths. The rule entries live in
``windvane.rules`` and the cold tier in ``windvane.archive``; both are mixed
into ``MemoryStore`` so every method keeps one home and one name.

Stdlib only. Embedding vectors are optional: they need the semantic tier
(``windvane.semantic``), which is the only place numpy is imported; without
it vectors are read and written as JSON, and a store that has none simply
searches by keyword and score.

The on-disk format is the one the store has always written, so a store
copied from an older install opens unchanged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Relevance at or above this never ages out of the hot tier, however long it
# sits untouched. It must stay ABOVE every default, or the exemption stops
# meaning "someone marked this important" and starts meaning "it exists":
# manual remember defaults to 5, and the miner mints auto-captured decisions
# at 7. While this was 7, every auto-captured decision was born permanently
# exempt and the hot tier could not shrink. Rules use 9 and are exempt by
# category anyway.
ARCHIVE_EXEMPT_RELEVANCE = 8

# Hot-path scoring/matching primitives live in windvane.hot_reader (read by
# the pre-edit hook without loading this module). Re-exported here: callers
# import them from the store.
from windvane.hot_reader import (  # noqa: E402,F401
    CATEGORY_BONUSES,
    RECENCY_HALF_LIFE_DAYS,
    SCORE_WEIGHTS,
    HotMemoryReader,
    _GENERIC_BASENAMES,
    _HOOK_TAG_PATTERNS,
    _file_match_score,
    extract_file_refs,
)


# ---------------------------------------------------------------------------
# The entry model
# ---------------------------------------------------------------------------


def _known(cls, data: dict) -> dict:
    """The keys of ``data`` that are fields of ``cls`` (extra keys a newer
    or older writer added are ignored, as they always were)."""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


@dataclass
class MemoryEntry:
    """A single memory entry."""

    content: str = ""
    # "rule", "mistake", "context", "discovery", "priority", "note", "decision", "lesson"
    category: str = "discovery"
    created_at: float = field(default_factory=time.time)
    source: Optional[str] = None  # what operation created this memory
    relevance: int = 5  # 1-10, higher = more important
    id: str = ""  # unique identifier (set on creation)
    last_accessed: float = field(default_factory=time.time)  # for decay tracking
    access_count: int = 1  # how often this memory was relevant
    tags: list = field(default_factory=list)  # auto-extracted: ["auth", "bootstrap"]
    related_files: list = field(default_factory=list)  # files this memory relates to
    cluster_id: Optional[str] = None  # which cluster this belongs to
    archived_at: Optional[float] = None  # when moved to the archive; None = active
    # Rules only: a hand-written detector (windvane.compliance). None = advisory.
    detector: Optional[dict] = None

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        d = _known(cls, data)
        d["content"] = str(d.get("content") or "")
        d["category"] = str(d.get("category") or "discovery")
        d["tags"] = list(d.get("tags") or [])
        d["related_files"] = list(d.get("related_files") or [])
        try:
            d["relevance"] = int(d.get("relevance", 5))
        except (TypeError, ValueError):
            d["relevance"] = 5
        for k in ("created_at", "last_accessed"):
            if k in d:
                try:
                    d[k] = float(d[k])
                except (TypeError, ValueError):
                    d.pop(k)
        return cls(**d)

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class MemoryCluster:
    """A group of related memories."""

    cluster_id: str = ""
    name: str = ""  # "Bootstrap Memories", "Auth Memories"
    memory_ids: list = field(default_factory=list)
    summary: str = ""
    tags: list = field(default_factory=list)  # common tags across memories
    created_at: float = field(default_factory=time.time)
    relevance: int = 5

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryCluster":
        return cls(**_known(cls, data))

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class ProjectMemory:
    """Memory about one project/directory."""

    project_path: str = ""
    project_name: str = ""
    summary: Optional[str] = None
    language: Optional[str] = None
    framework: Optional[str] = None
    key_files: dict = field(default_factory=dict)  # path -> description
    key_directories: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)  # list[MemoryEntry]
    recent_searches: list = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)
    file_memory_index: dict = field(default_factory=dict)  # file -> memory IDs
    tag_memory_index: dict = field(default_factory=dict)  # tag -> memory IDs
    clusters: dict = field(default_factory=dict)  # cluster_id -> MemoryCluster
    last_cleanup: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectMemory":
        d = _known(cls, data)
        d["entries"] = [
            e if isinstance(e, MemoryEntry) else MemoryEntry.from_dict(e)
            for e in (d.get("entries") or [])
            if isinstance(e, (dict, MemoryEntry))
        ]
        d["clusters"] = {
            k: c if isinstance(c, MemoryCluster) else MemoryCluster.from_dict(c)
            for k, c in (d.get("clusters") or {}).items()
            if isinstance(c, (dict, MemoryCluster))
        }
        for k in ("key_files", "key_directories", "file_memory_index", "tag_memory_index"):
            d[k] = dict(d.get(k) or {})
        d["recent_searches"] = list(d.get("recent_searches") or [])
        return cls(**d)

    def model_dump(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# The tool response every windvane tool answers with
# ---------------------------------------------------------------------------


@dataclass
class WorkLog:
    """What a tool did during one operation."""

    what_i_tried: list = field(default_factory=list)
    what_worked: list = field(default_factory=list)
    what_failed: list = field(default_factory=list)
    files_examined: int = 0
    time_taken_ms: int = 0


@dataclass
class Response:
    """A tool's answer: a status, the reasoning the model reads, a data
    payload, and warnings / suggestions / questions. ``to_formatted_string``
    renders the compact text the tools return."""

    status: str = "success"  # success, partial, failed, needs_clarification, not_found
    work_log: WorkLog = field(default_factory=WorkLog)
    confidence: str = "medium"
    reasoning: str = ""
    data: Any = None
    questions: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_formatted_string(self) -> str:
        """The response as the text a model reads: status and reasoning,
        warnings, the data compacted, questions, two suggestions at most,
        and the failures."""
        lines = []
        if self.status in ("failed", "needs_clarification"):
            lines.append(f"{self.status}: {self.reasoning}")
        elif self.reasoning:
            lines.append(self.reasoning)

        for w in self.warnings:
            lines.append(f"  ! {w}")

        if self.data:
            if isinstance(self.data, dict):
                for key, value in self.data.items():
                    if isinstance(value, list):
                        for item in value[:10]:
                            if isinstance(item, dict):
                                # Memory entries: [id] (relevance) content #tags
                                mid = item.get("id", "")
                                content = item.get("content", str(item))
                                rel = item.get("relevance", "")
                                tags = " ".join(f"#{t}" for t in item.get("tags", [])[:3])
                                lines.append(f"[{mid}] ({rel}) {str(content)[:100]} {tags}".rstrip())
                            else:
                                lines.append(f"  {item}")
                    elif isinstance(value, dict):
                        flat = ", ".join(
                            f"{k}={v}" for k, v in value.items() if v is not None and v != "" and v != []
                        )
                        if flat:
                            lines.append(f"{key}: {flat}")
                    elif value is not None and value != "" and value != []:
                        lines.append(f"{key}: {value}")
            else:
                lines.append(str(self.data))

        for q in self.questions:
            lines.append(f"? {q}")
        for s in self.suggestions[:2]:
            lines.append(f"  > {s}")
        if self.work_log.what_failed:
            lines.append(f"FAILED: {', '.join(self.work_log.what_failed)}")
        return "\n".join(lines)


def project_store_dir(project_path: str, storage_dir: str = "") -> Path:
    """A project's directory in the store (``storage_dir``, default the
    configured one), registered or not (registering nothing): the manifest's
    hash when the project is registered, else the hash a registration would
    give it."""
    from windvane.config import store_dir

    root = Path(storage_dir).expanduser() if storage_dir else store_dir()
    norm = MemoryStore._normalize_path(project_path)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        info = (manifest.get("projects") or {}).get(norm)
    except Exception:
        info = None
    hash_id = info["hash"] if info else hashlib.md5(norm.encode()).hexdigest()[:8]
    return root / "projects" / hash_id


def _numpy():
    """numpy when the semantic tier is on, else None. The core never imports
    numpy itself; ``windvane.semantic`` decides."""
    try:
        from windvane import semantic

        get = getattr(semantic, "numpy_module", None)
        return get() if callable(get) else None
    except Exception:
        return None


def _embed_signature() -> tuple[str, str]:
    """(current signature, legacy signature) of the embedding model."""
    try:
        from windvane.semantic.config import LEGACY_SIGNATURE, embed_signature

        return embed_signature(), LEGACY_SIGNATURE
    except Exception:
        return "", ""


from windvane.archive import ArchiveMixin  # noqa: E402
from windvane.rules import RulesMixin  # noqa: E402


class MemoryStore(RulesMixin, ArchiveMixin):
    """windvane's memory: knowledge that persists across sessions, with
    auto-tagging, deduplication, contextual search and scored injection."""

    TAG_PATTERNS = {
        r"BOOTSTRAP|bootstrap": "bootstrap",
        r"ROUND\s*\d+|round-?\d+": "bootstrap",
        r"MISTAKE|mistake": "mistake",
        r"DECISION|decision": "decision",
        r"\bauth\b|login|password|authentication|authorization": "auth",
        r"test|pytest|unittest|jest|mocha": "testing",
        r"config|settings|\.env": "config",
        r"database|db|sql|migration": "database",
        r"api|endpoint|route|handler": "api",
        r"security|vulnerability|CVE": "security",
        r"performance|optimize|slow|fast": "performance",
        r"bug|fix|error|crash": "bugfix",
        r"refactor|cleanup|improve": "refactor",
        r"install|setup|dependency": "setup",
    }

    def __init__(self, storage_dir: str = ""):
        if not storage_dir:
            from windvane.config import store_dir

            storage_dir = str(store_dir())
        self.storage_dir = Path(storage_dir).expanduser()
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self._manifest_file = self.storage_dir / "manifest.json"
        self._global_file = self.storage_dir / "global.json"
        self._projects_dir = self.storage_dir / "projects"
        self._manifest: dict = {"version": 3, "projects": {}}
        self._dirty_projects: set[str] = set()  # norm paths that need saving
        self._manifest_dirty: bool = False

        # Single-file layout of the first versions (migrated on load).
        self.memory_file = self.storage_dir / "memory.json"
        self.archive_file = self.storage_dir / "archive.json"

        self._projects: dict[str, ProjectMemory] = {}
        # Disk stamp (mtime_ns, size) of each loaded memory.json and of the
        # manifest. A long-lived process (the daemon) otherwise serves a copy
        # taken at first touch and, on its next save, overwrites what a CLI
        # or a hook wrote in the meantime.
        self._project_stamps: dict[str, tuple[int, int]] = {}
        self._manifest_stamp: tuple[int, int] = (0, 0)
        self._global_entries: list[MemoryEntry] = []
        self._load_error: Optional[str] = None
        self._save_error: Optional[str] = None

        # The cold tier (lazy-loaded, never on the hot path).
        self._archive_projects: dict[str, ProjectMemory] = {}
        self._archive_loaded: bool = False
        try:
            self.archive_after_days: int = int(os.environ.get("WINDVANE_ARCHIVE_DAYS", "14"))
        except ValueError:
            self.archive_after_days = 14

        # Optional embedding vectors (per project).
        self._embeddings_file = self.storage_dir / "embeddings.json"  # legacy global file
        self._embeddings: dict[str, list[float]] = {}
        self._embeddings_loaded: bool = False
        self._np = _numpy()

        self._load()

    # ------------------------------------------------------------------
    # Layout, load and save
    # ------------------------------------------------------------------

    def _project_dir(self, norm_path: str) -> Path:
        """The per-project directory from the manifest (registered here when new)."""
        manifest_projects = self._manifest.get("projects", {})
        if norm_path in manifest_projects:
            hash_id = manifest_projects[norm_path]["hash"]
        else:
            hash_id = hashlib.md5(norm_path.encode()).hexdigest()[:8]
            manifest_projects[norm_path] = {"hash": hash_id, "name": Path(norm_path).name}
            self._manifest["projects"] = manifest_projects
            self._manifest_dirty = True
        proj_dir = self._projects_dir / hash_id
        proj_dir.mkdir(parents=True, exist_ok=True)
        return proj_dir

    def _load_project(self, norm_path: str) -> ProjectMemory:
        """Load one project from its directory."""
        pdir = self._project_dir(norm_path)
        mem_file = pdir / "memory.json"
        if mem_file.exists():
            try:
                proj_data = json.loads(mem_file.read_text(encoding="utf-8"))
                proj_data["project_path"] = norm_path
                proj_data.setdefault("project_name", Path(norm_path).name)
                proj = ProjectMemory.from_dict(proj_data)
                dirty = False
                for entry in proj.entries:
                    if not entry.id:
                        entry.id = self._generate_entry_id(entry.content, project_path=norm_path)
                        dirty = True
                if dirty:
                    self._projects[norm_path] = proj
                    self._save_project(norm_path)
                self._merge_pending_embeddings(norm_path)
                return proj
            except Exception:
                pass
        return ProjectMemory(project_path=norm_path, project_name=Path(norm_path).name)

    def _merge_pending_embeddings(self, norm_path: str):
        """Merge a project's pending vectors into its main vectors.

        Pending files are signature-stamped ({"model": sig, "vectors": {...}};
        a flat {id: vec} dict counts as the legacy model). Vectors from
        another model than the configured one are dropped, not merged:
        embed_all_memories re-embeds them in the current space."""
        sig, legacy = _embed_signature()
        if not sig:
            return
        pdir = self._project_dir(norm_path)
        pending_file = pdir / "embeddings_pending.json"
        if not pending_file.exists():
            return
        try:
            raw = json.loads(pending_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "vectors" in raw:
                pending_sig = raw.get("model", legacy)
                pending = raw.get("vectors", {})
            else:
                pending_sig = legacy
                pending = raw
            if pending_sig != sig:
                pending_file.unlink(missing_ok=True)
                return
            if not pending:
                return

            if self._np is not None:
                npy_file = pdir / "embeddings.npy"
                index_file = pdir / "embeddings_index.json"
                ids: list = []
                matrix = None
                if npy_file.exists() and index_file.exists():
                    idx_data = json.loads(index_file.read_text(encoding="utf-8"))
                    ids = idx_data.get("ids", [])
                    stamp = idx_data.get("model", legacy)
                    loaded = self._np.load(str(npy_file))
                    if stamp == sig and len(loaded.shape) == 2 and loaded.shape[0] == len(ids):
                        matrix = loaded
                    else:
                        ids = []
                existing_set = set(ids)
                new_ids, new_vecs = [], []
                for mid, vec in pending.items():
                    if mid not in existing_set:
                        new_ids.append(mid)
                        new_vecs.append(vec)
                if new_ids:
                    new_arr = self._np.array(new_vecs, dtype=self._np.float32)
                    if matrix is not None and len(matrix) > 0 and matrix.shape[1] != new_arr.shape[1]:
                        pending_file.unlink(missing_ok=True)
                        return
                    if matrix is not None and len(matrix) > 0:
                        matrix = self._np.vstack([matrix, new_arr])
                    else:
                        matrix = new_arr
                    ids.extend(new_ids)
                    tmp_npy = pdir / "embeddings_tmp"
                    self._np.save(str(tmp_npy), matrix)  # writes embeddings_tmp.npy
                    (pdir / "embeddings_tmp.npy").replace(npy_file)
                    temp = index_file.with_suffix(".json.tmp")
                    temp.write_text(json.dumps({"ids": ids, "model": sig}), encoding="utf-8")
                    temp.replace(index_file)
            else:
                emb_file = pdir / "embeddings.json"
                existing = {}
                if emb_file.exists():
                    existing = json.loads(emb_file.read_text(encoding="utf-8"))
                existing.update(pending)
                temp = emb_file.with_suffix(".json.tmp")
                temp.write_text(json.dumps(existing), encoding="utf-8")
                temp.replace(emb_file)
            pending_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _load(self):
        """Load the manifest and the global entries; projects load lazily."""
        if self.memory_file.exists() and not self._manifest_file.exists():
            self._migrate_to_v3()
            return
        if self._manifest_file.exists():
            try:
                self._manifest = json.loads(self._manifest_file.read_text(encoding="utf-8"))
                self._manifest_stamp = self._disk_stamp(self._manifest_file)
            except Exception as e:
                self._load_error = f"Manifest corrupted, starting fresh: {e}"
                self._manifest = {"version": 3, "projects": {}}
        if self._global_file.exists():
            try:
                global_data = json.loads(self._global_file.read_text(encoding="utf-8"))
                for entry_data in global_data.get("entries", []):
                    self._global_entries.append(MemoryEntry.from_dict(entry_data))
            except Exception:
                pass

    def _migrate_to_v3(self):
        """Migrate the old single-file memory.json to the per-project layout."""
        try:
            data = json.loads(self.memory_file.read_text(encoding="utf-8"))
            version = data.get("version", 1)
            self._projects_dir.mkdir(parents=True, exist_ok=True)

            for path, proj_data in data.get("projects", {}).items():
                if version == 1:
                    proj_data = self._migrate_project_v1_to_v2(proj_data)
                norm_path = self._normalize_path(path)
                proj_data["project_path"] = norm_path
                proj_data.setdefault("project_name", Path(norm_path).name)
                if norm_path in self._projects:
                    existing = self._projects[norm_path]
                    new_proj = ProjectMemory.from_dict(proj_data)
                    existing_ids = {e.id for e in existing.entries}
                    for entry in new_proj.entries:
                        if entry.id not in existing_ids:
                            existing.entries.append(entry)
                else:
                    self._projects[norm_path] = ProjectMemory.from_dict(proj_data)
                    self._dirty_projects.add(norm_path)
                hash_id = hashlib.md5(norm_path.encode()).hexdigest()[:8]
                self._manifest["projects"][norm_path] = {"hash": hash_id, "name": Path(norm_path).name}

            for entry_data in data.get("global", []):
                if version == 1:
                    entry_data = self._migrate_entry_v1_to_v2(entry_data)
                self._global_entries.append(MemoryEntry.from_dict(entry_data))

            legacy_emb_file = self.storage_dir / "embeddings.json"
            legacy_embeddings = {}
            if legacy_emb_file.exists():
                try:
                    legacy_embeddings = json.loads(legacy_emb_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            for norm_path, proj in self._projects.items():
                pdir = self._project_dir(norm_path)
                proj_data = proj.model_dump()
                proj_data.pop("project_path", None)
                temp = (pdir / "memory.json").with_suffix(".json.tmp")
                temp.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")
                temp.replace(pdir / "memory.json")
                proj_emb_ids, proj_emb_vecs = [], []
                for entry in proj.entries:
                    if entry.id in legacy_embeddings:
                        proj_emb_ids.append(entry.id)
                        proj_emb_vecs.append(legacy_embeddings[entry.id])
                if proj_emb_ids:
                    self._save_project_embeddings(norm_path, proj_emb_ids, proj_emb_vecs)

            self._dirty_projects.clear()
            self._save_global()
            self._manifest_dirty = True
            self._save_manifest()

            try:
                self.memory_file.replace(self.memory_file.with_suffix(".json.v2backup"))
            except Exception:
                pass
            if legacy_emb_file.exists():
                try:
                    legacy_emb_file.replace(legacy_emb_file.with_suffix(".json.v2backup"))
                except Exception:
                    pass
        except Exception as e:
            self._load_error = f"Migration failed: {e}"
            try:
                self.memory_file.replace(self.memory_file.with_suffix(".json.corrupted"))
            except Exception:
                pass

    def _migrate_entry_v1_to_v2(self, entry_data: dict) -> dict:
        content = entry_data.get("content", "")
        if not entry_data.get("id"):
            created = str(entry_data.get("created_at", ""))
            entry_data["id"] = hashlib.md5(f"{content}{created}".encode()).hexdigest()[:12]
        if "tags" not in entry_data:
            entry_data["tags"] = self._extract_tags(content)
        if "related_files" not in entry_data:
            entry_data["related_files"] = self._extract_file_refs(content)
        if "last_accessed" not in entry_data:
            entry_data["last_accessed"] = entry_data.get("created_at", time.time())
        if "access_count" not in entry_data:
            entry_data["access_count"] = 1
        return entry_data

    def _migrate_project_v1_to_v2(self, proj_data: dict) -> dict:
        entries = proj_data.get("entries", [])
        for i, entry in enumerate(entries):
            entries[i] = self._migrate_entry_v1_to_v2(entry)
        file_index: dict = defaultdict(list)
        tag_index: dict = defaultdict(list)
        for entry in entries:
            entry_id = entry.get("id", "")
            for f in entry.get("related_files", []):
                if entry_id not in file_index[f]:
                    file_index[f].append(entry_id)
            for t in entry.get("tags", []):
                if entry_id not in tag_index[t]:
                    tag_index[t].append(entry_id)
        proj_data["file_memory_index"] = dict(file_index)
        proj_data["tag_memory_index"] = dict(tag_index)
        proj_data["clusters"] = proj_data.get("clusters", {})
        proj_data["last_cleanup"] = proj_data.get("last_cleanup", time.time())
        return proj_data

    def _extract_tags(self, content: str) -> list[str]:
        """Tags from the content by pattern."""
        tags = set()
        for pattern, tag in self.TAG_PATTERNS.items():
            if re.search(pattern, content, re.IGNORECASE):
                tags.add(tag)
        round_match = re.search(r"round\s*(\d+)", content.lower())
        if round_match:
            tags.add(f"round-{round_match.group(1)}")
        return list(tags)

    def _extract_file_refs(self, content: str) -> list[str]:
        """File references in the content (full paths kept)."""
        return extract_file_refs(content)

    def _generate_entry_id(self, content: str, project_path: str = "") -> str:
        """An id from the content, unique within the target project."""
        base_id = hashlib.md5(content.encode()).hexdigest()[:12]
        check_ids = set()
        if project_path:
            norm = self._normalize_path(project_path)
            proj = self._projects.get(norm)
            if proj:
                check_ids.update(e.id for e in proj.entries)
        check_ids.update(e.id for e in self._global_entries)
        if base_id not in check_ids:
            return base_id
        counter = 1
        while f"{base_id}_{counter}" in check_ids:
            counter += 1
        return f"{base_id}_{counter}"

    def _is_duplicate(self, content: str, entries: list, threshold: float = 0.85) -> Optional[MemoryEntry]:
        """The existing entry ``content`` duplicates (word-set Jaccard at
        ``threshold``), or None."""
        new_words = set(content.lower().split())
        if not new_words:
            return None
        for entry in entries:
            existing_words = set(entry.content.lower().split())
            if not existing_words:
                continue
            union = len(new_words | existing_words)
            if union > 0 and len(new_words & existing_words) / union >= threshold:
                return entry
        return None

    def _update_indexes(self, proj: ProjectMemory, entry: MemoryEntry):
        for f in entry.related_files:
            proj.file_memory_index.setdefault(f, [])
            if entry.id not in proj.file_memory_index[f]:
                proj.file_memory_index[f].append(entry.id)
        for t in entry.tags:
            proj.tag_memory_index.setdefault(t, [])
            if entry.id not in proj.tag_memory_index[t]:
                proj.tag_memory_index[t].append(entry.id)

    def _rebuild_indexes(self, proj: ProjectMemory):
        proj.file_memory_index = {}
        proj.tag_memory_index = {}
        for entry in proj.entries:
            self._update_indexes(proj, entry)

    def _save_manifest(self):
        if not self._manifest_dirty:
            return
        try:
            temp = self._manifest_file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(self._manifest, indent=2), encoding="utf-8")
            temp.replace(self._manifest_file)
            self._manifest_dirty = False
            self._manifest_stamp = self._disk_stamp(self._manifest_file)
        except Exception as e:
            self._save_error = f"Failed to save manifest: {e}"

    def _save_global(self):
        if not self._global_entries and not self._global_file.exists():
            return
        data = {"entries": [e.model_dump() for e in self._global_entries]}
        try:
            temp = self._global_file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp.replace(self._global_file)
        except Exception as e:
            self._save_error = f"Failed to save global: {e}"

    def _save_project(self, norm_path: str) -> bool:
        """Save one project.

        Another process may have written the file since this one loaded it.
        A project this process did not mutate is then not written (the
        ``_save()`` all-loaded fallback is how a stale copy used to land on a
        fresh file). A project this process DID mutate on a stale base is
        merged by entry id: our copy plus any entry on disk we never saw.
        That can resurrect an entry the other writer deleted in the same
        instant; it can never lose a write."""
        proj = self._projects.get(norm_path)
        if not proj:
            return False
        if norm_path in self._project_stamps and self._project_stale(norm_path):
            if norm_path not in self._dirty_projects:
                return True
            try:
                disk = self._load_project(norm_path)
                seen = {e.id for e in proj.entries}
                added = [e for e in disk.entries if e.id and e.id not in seen]
                if added:
                    proj.entries.extend(added)
                    self._rebuild_indexes(proj)
            except Exception:
                pass
        try:
            pdir = self._project_dir(norm_path)
            proj_data = proj.model_dump()
            proj_data.pop("project_path", None)
            temp = (pdir / "memory.json").with_suffix(".json.tmp")
            temp.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")
            temp.replace(pdir / "memory.json")
            self._project_stamps[norm_path] = self._disk_stamp(pdir / "memory.json")
            return True
        except Exception as e:
            self._save_error = f"Failed to save project {norm_path}: {e}"
            return False

    def _save(self) -> bool:
        """Save the dirty projects (all loaded ones when none is marked, for
        callers that do not mark), the global entries and the manifest."""
        ok = True
        try:
            projects_to_save = self._dirty_projects if self._dirty_projects else set(self._projects.keys())
            for norm_path in list(projects_to_save):
                if not self._save_project(norm_path):
                    ok = False
            self._dirty_projects.clear()
            self._save_global()
            self._save_manifest()
            return ok
        except Exception as e:
            self._save_error = f"Failed to save memory: {e}"
            return False

    @staticmethod
    def _normalize_path(path: str) -> str:
        """A project path as a dict key: resolved, forward slashes, lower-case drive."""
        normalized = str(Path(path).resolve()).replace("\\", "/")
        if len(normalized) >= 2 and normalized[1] == ":":
            normalized = normalized[0].lower() + normalized[1:]
        return normalized

    @staticmethod
    def _disk_stamp(path: Path) -> tuple[int, int]:
        try:
            st = path.stat()
            return (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return (0, 0)

    def _refresh_manifest_if_changed(self) -> None:
        """Another writer may have registered a project since we started."""
        stamp = self._disk_stamp(self._manifest_file)
        if stamp != self._manifest_stamp and stamp != (0, 0):
            try:
                data = json.loads(self._manifest_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._manifest = data
            except Exception:
                return
            self._manifest_stamp = stamp

    def _project_stale(self, norm_path: str) -> bool:
        """Has memory.json changed on disk since this process loaded it?"""
        stamp = self._disk_stamp(self._project_dir(norm_path) / "memory.json")
        return stamp != self._project_stamps.get(norm_path, (0, 0))

    def _load_project_tracked(self, norm_path: str) -> ProjectMemory:
        proj = self._load_project(norm_path)
        self._project_stamps[norm_path] = self._disk_stamp(self._project_dir(norm_path) / "memory.json")
        return proj

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def register_project(self, project_path: str) -> Path:
        """Make ``project_path`` a project of the store: its manifest row and
        its directory, persisted at once. Returns the directory. A project
        whose first record is a checkpoint, not a memory, needs the ring
        before anything is remembered about it."""
        pdir = self._project_dir(self._normalize_path(project_path))
        self._save_manifest()
        return pdir

    def get_project(self, project_path: str) -> Optional[ProjectMemory]:
        """A project's memory, lazy-loaded, and reloaded when the file on
        disk is newer than the copy held here."""
        project_path = self._normalize_path(project_path)
        if project_path in self._projects:
            if self._project_stale(project_path):
                self._projects[project_path] = self._load_project_tracked(project_path)
            return self._projects.get(project_path)
        if project_path not in self._manifest.get("projects", {}):
            self._refresh_manifest_if_changed()
        if project_path in self._manifest.get("projects", {}):
            self._projects[project_path] = self._load_project_tracked(project_path)
            return self._projects.get(project_path)
        return None

    def remember_project(
        self,
        project_path: str,
        summary: Optional[str] = None,
        language: Optional[str] = None,
        framework: Optional[str] = None,
    ) -> ProjectMemory:
        """Create or update a project's memory (registering it)."""
        project_path = self._normalize_path(project_path)
        if self.get_project(project_path) is None:
            self._projects[project_path] = ProjectMemory(
                project_path=project_path, project_name=Path(project_path).name
            )
            self._project_dir(project_path)
        proj = self._projects[project_path]
        if summary:
            proj.summary = summary
        if language:
            proj.language = language
        if framework:
            proj.framework = framework
        proj.last_updated = time.time()
        self._dirty_projects.add(project_path)
        self._save()
        return proj

    def remember_key_file(self, project_path: str, file_path: str, description: str):
        proj = self.remember_project(project_path)
        proj.key_files[file_path] = description
        proj.last_updated = time.time()
        self._dirty_projects.add(proj.project_path)
        self._save()

    def remember_discovery(
        self,
        project_path: str,
        content: str,
        source: Optional[str] = None,
        relevance: int = 5,
        tags: Optional[list] = None,
        related_files: Optional[list] = None,
        category: str = "discovery",
        auto_embed: bool = True,  # False for bulk inserts, then embed_all_memories
    ) -> tuple[bool, str]:
        """Remember something about a project. Returns (added, message)."""
        proj = self.remember_project(project_path)
        duplicate = self._is_duplicate(content, proj.entries)
        if duplicate:
            duplicate.access_count += 1
            duplicate.last_accessed = time.time()
            if relevance > duplicate.relevance:
                duplicate.relevance = relevance
            self._dirty_projects.add(proj.project_path)
            self._save()
            return (False, f"Duplicate of existing memory (id={duplicate.id}), updated access count")
        if category == "mistake":
            # An acknowledged mistake lives in the archive. The same mistake
            # logged again must not come back as a fresh hot entry and reappear
            # in the pre-edit banners; it stays where the acknowledgement put it.
            self._load_archive()
            archived = self._archive_projects.get(proj.project_path)
            shelved = self._is_duplicate(content, archived.entries) if archived else None
            if shelved:
                shelved.access_count += 1
                shelved.last_accessed = time.time()
                self._save_archive()
                return (False, f"Duplicate of archived memory (id={shelved.id}), left in the archive")

        auto_tags = self._extract_tags(content)
        auto_files = self._extract_file_refs(content)
        entry = MemoryEntry(
            id=self._generate_entry_id(content, project_path=project_path),
            content=content,
            category=category,
            source=source,
            relevance=relevance,
            tags=list(set((tags or []) + auto_tags)),
            related_files=list(set((related_files or []) + auto_files)),
        )
        proj.entries.append(entry)
        self._update_indexes(proj, entry)
        proj.last_updated = time.time()
        self._dirty_projects.add(proj.project_path)
        self._save()

        if auto_embed:
            try:
                self.embed_memory(entry.id, entry.content, project_path=project_path)
            except Exception:
                pass  # vectors are optional
        return (True, f"Memory added with id={entry.id}, tags={entry.tags}")

    def add_priority(self, content: str, project_path: Optional[str] = None, relevance: int = 8):
        """A priority note (something important to remember)."""
        entry = MemoryEntry(content=content, category="priority", relevance=relevance)
        if project_path:
            proj = self.remember_project(project_path)
            entry.id = self._generate_entry_id(content, project_path=project_path)
            proj.entries.append(entry)
            proj.last_updated = time.time()
            self._dirty_projects.add(proj.project_path)
        else:
            entry.id = self._generate_entry_id(content)
            self._global_entries.append(entry)
        self._save()

    def log_search(self, project_path: str, query: str, results_count: int, top_files: list):
        """Log a search so a later one can skip the same work."""
        proj = self.remember_project(project_path)
        proj.recent_searches = proj.recent_searches[-19:]
        proj.recent_searches.append(
            {"query": query, "results_count": results_count, "top_files": top_files[:5], "timestamp": time.time()}
        )
        self._dirty_projects.add(proj.project_path)
        self._save()

    def recall(self, project_path: Optional[str] = None, category: Optional[str] = None, limit: int = 20) -> dict:
        """What we know: the global priorities and a project's memories."""
        result: dict = {"global_priorities": [], "project": None}
        priorities = sorted(
            [e for e in self._global_entries if e.category == "priority"], key=lambda x: x.relevance, reverse=True
        )
        result["global_priorities"] = [{"content": e.content, "relevance": e.relevance} for e in priorities[:5]]
        if project_path:
            project_path = self._normalize_path(project_path)
        proj = self.get_project(project_path) if project_path else None
        if proj:
            entries = proj.entries
            if category:
                entries = [e for e in entries if e.category == category]
            entries = sorted(entries, key=lambda x: x.relevance, reverse=True)
            result["project"] = {
                "name": proj.project_name,
                "summary": proj.summary,
                "language": proj.language,
                "framework": proj.framework,
                "key_files": proj.key_files,
                "key_directories": proj.key_directories,
                "discoveries": [
                    {
                        "id": e.id,
                        "content": e.content,
                        "relevance": e.relevance,
                        "category": e.category,
                        "created_at": e.created_at,
                    }
                    for e in entries[:limit]
                ],
                "recent_searches": proj.recent_searches[-5:],
            }
        return result

    def forget_project(self, project_path: str):
        """Clear a project's memory: its directory and its manifest row."""
        norm = self._normalize_path(project_path)
        self._projects.pop(norm, None)
        self._project_stamps.pop(norm, None)
        self._dirty_projects.discard(norm)
        if norm in self._manifest.get("projects", {}):
            hash_id = self._manifest["projects"][norm]["hash"]
            proj_dir = self._projects_dir / hash_id
            if proj_dir.exists():
                import shutil

                try:
                    shutil.rmtree(str(proj_dir))
                except Exception:
                    pass
            del self._manifest["projects"][norm]
            self._manifest_dirty = True
            self._save_manifest()
        # The archive keeps its own rows per project; forgetting a project
        # forgets what was archived from it too.
        self._load_archive()
        if self._archive_projects.pop(norm, None) is not None:
            self._save_archive()

    def clear_all(self):
        """Clear every loaded memory (use with caution)."""
        self._projects = {}
        self._global_entries = []
        self._save()

    def get_stats(self) -> dict:
        """Counts, from the manifest without loading every project."""
        loaded_entries = sum(len(p.entries) for p in self._projects.values())
        manifest_projects = self._manifest.get("projects", {})
        unloaded_entries = 0
        for norm_path, info in manifest_projects.items():
            if norm_path not in self._projects:
                mem_file = self._projects_dir / info["hash"] / "memory.json"
                if mem_file.exists():
                    try:
                        pdata = json.loads(mem_file.read_text(encoding="utf-8"))
                        unloaded_entries += len(pdata.get("entries", []))
                    except Exception:
                        pass
        stats = {
            "projects_tracked": len(manifest_projects),
            "total_entries": loaded_entries + unloaded_entries + len(self._global_entries),
            "storage_path": str(self.storage_dir),
        }
        # global.json is a legacy cross-project store nothing writes any more:
        # surface its count only when an older install has entries there.
        if self._global_entries:
            stats["global_entries"] = len(self._global_entries)
        if self._load_error:
            stats["load_error"] = self._load_error
        if self._save_error:
            stats["save_error"] = self._save_error
        return stats

    def get_health(self) -> dict:
        health = {
            "healthy": not (self._load_error or self._save_error),
            "storage_path": str(self.storage_dir),
            "manifest_exists": self._manifest_file.exists(),
            "projects_dir_exists": self._projects_dir.exists(),
            "storage_version": self._manifest.get("version", "unknown"),
        }
        if self._load_error:
            health["load_error"] = self._load_error
        if self._save_error:
            health["save_error"] = self._save_error
        return health

    def clear_errors(self):
        self._load_error = None
        self._save_error = None

    def get_memory_summary(self, project_path: str) -> dict:
        """Counts by category, stale entries and suggestions, for a banner."""
        proj = self.get_project(project_path)
        if not proj:
            return {"total": 0, "categories": {}, "stale": [], "suggestions": []}
        now = time.time()
        categories: dict = {}
        stale = []
        suggestions = []
        for entry in proj.entries:
            categories[entry.category] = categories.get(entry.category, 0) + 1
            age_days = (now - entry.last_accessed) / 86400
            if age_days > 60 and entry.category not in ("rule", "mistake"):
                stale.append(
                    {
                        "id": entry.id,
                        "age_days": int(age_days),
                        "category": entry.category,
                        "preview": entry.content[:50] + "..." if len(entry.content) > 50 else entry.content,
                    }
                )
        decision_count = 0
        recent_decisions = []
        for entry in proj.entries:
            if entry.content.upper().startswith("DECISION:") or entry.category == "decision":
                decision_count += 1
                age_hours = (now - entry.created_at) / 3600
                if age_hours < 24:
                    content = entry.content
                    if content.upper().startswith("DECISION:"):
                        content = content[9:].strip()
                    recent_decisions.append({"content": content[:100], "age_hours": int(age_hours)})
        if len(stale) > 3:
            suggestions.append(
                f"{len(stale)} memories haven't been accessed in 60+ days - consider reviewing with memory(archive)"
            )
        if categories.get("discovery", 0) > 20:
            suggestions.append("Many discoveries stored - consider promoting important ones to rules")
        if categories.get("mistake", 0) > 10:
            suggestions.append("Many mistakes stored - review whether a pattern has emerged")
        total = len(proj.entries)
        if total > 30:
            suggestions.append("Manage memories: memory(modify/delete/promote, memory_id='...')")
        return {
            "total": total,
            "categories": categories,
            "stale_count": len(stale),
            "stale": stale[:5],
            "decision_count": decision_count,
            "recent_decisions": recent_decisions[:3],
            "suggestions": suggestions,
        }

    def get_memories_for_files(
        self,
        project_path: str,
        file_paths: list,
        include_rules: bool = True,
        include_mistakes: bool = True,
    ) -> dict:
        """Memories related to some files: rules, mistakes, file memories, other."""
        proj = self.get_project(project_path)
        if not proj:
            return {"rules": [], "mistakes": [], "file_memories": [], "other": []}
        rules, mistakes, file_memories, other = [], [], [], []
        file_names = set(Path(f).name for f in file_paths)
        file_paths_set = set(file_paths)
        for entry in proj.entries:
            content = entry.content
            if entry.category == "rule":
                rules.append(entry)
                continue
            if content.upper().startswith("MISTAKE:") or entry.category == "mistake":
                mistakes.append(entry)
                continue
            related = any(rf in file_paths_set or Path(rf).name in file_names for rf in entry.related_files)
            if not related:
                related = any(fn in content for fn in file_names)
            (file_memories if related else other).append(entry)
        return {"rules": rules, "mistakes": mistakes, "file_memories": file_memories, "other": other}

    # ------------------------------------------------------------------
    # Search, recent, modify, delete, promote
    # ------------------------------------------------------------------

    def search_memories(
        self,
        project_path: str,
        file_path: Optional[str] = None,
        tags: Optional[list] = None,
        query: Optional[str] = None,
        limit: int = 5,
    ) -> list:
        """Memories by file, by tag, or by keyword; the most relevant first."""
        proj = self.get_project(project_path)
        if not proj:
            return []
        results = []
        seen_ids: set = set()
        if file_path:
            file_name = Path(file_path).name
            for f in [file_path, file_name]:
                for entry_id in proj.file_memory_index.get(f, []):
                    if entry_id not in seen_ids:
                        entry = self._get_entry_by_id(proj, entry_id)
                        if entry:
                            results.append(entry)
                            seen_ids.add(entry_id)
        if tags:
            for tag in tags:
                for entry_id in proj.tag_memory_index.get(tag, []):
                    if entry_id not in seen_ids:
                        entry = self._get_entry_by_id(proj, entry_id)
                        if entry:
                            results.append(entry)
                            seen_ids.add(entry_id)
        if query:
            query_words = set(query.lower().split())
            for entry in proj.entries:
                if entry.id in seen_ids:
                    continue
                if query_words & set(entry.content.lower().split()):
                    results.append(entry)
                    seen_ids.add(entry.id)
        for entry in results:
            entry.last_accessed = time.time()
            entry.access_count += 1
        results = sorted(results, key=lambda x: x.relevance, reverse=True)[:limit]
        if results:
            self._dirty_projects.add(proj.project_path)
            self._save()
        return results

    def _get_entry_by_id(self, proj: ProjectMemory, entry_id: str) -> Optional[MemoryEntry]:
        for entry in proj.entries:
            if entry.id == entry_id:
                return entry
        return None

    def get_contextual_memories(self, project_path: str, file_path: str, limit: int = 3) -> list:
        """Memories for a file being edited: by the file and the tags its path implies."""
        path_lower = file_path.lower()
        inferred_tags = [tag for pattern, tag in self.TAG_PATTERNS.items() if re.search(pattern, path_lower)]
        return self.search_memories(
            project_path=project_path, file_path=file_path, tags=inferred_tags or None, limit=limit
        )

    def get_recent_memories(self, project_path: str, category: Optional[str] = None, limit: int = 10) -> list:
        """The newest memories first."""
        proj = self.get_project(project_path)
        if not proj:
            return []
        entries = proj.entries
        if category:
            entries = [e for e in entries if e.category == category]
        return sorted(entries, key=lambda x: x.created_at, reverse=True)[:limit]

    def modify_memory(
        self,
        project_path: str,
        memory_id: str,
        content: Optional[str] = None,
        relevance: Optional[int] = None,
        category: Optional[str] = None,
    ) -> tuple[bool, str]:
        proj = self.get_project(project_path)
        if not proj:
            return (False, "Project not found")
        entry = self._get_entry_by_id(proj, memory_id)
        if not entry:
            return (False, f"Memory {memory_id} not found")
        changes = []
        if content is not None:
            entry.content = content
            entry.tags = self._extract_tags(content)
            entry.related_files = self._extract_file_refs(content)
            changes.append("content")
        if relevance is not None:
            entry.relevance = relevance
            changes.append("relevance")
        if category is not None:
            entry.category = category
            changes.append("category")
        if changes:
            if "content" in changes:
                self._rebuild_indexes(proj)
            self._dirty_projects.add(proj.project_path)
            self._save()
            return (True, f"Modified: {', '.join(changes)}")
        return (False, "No changes specified")

    def delete_memory(self, project_path: str, memory_id: str) -> tuple[bool, str]:
        proj = self.get_project(project_path)
        if not proj:
            return (False, "Project not found")
        entry = self._get_entry_by_id(proj, memory_id)
        if not entry:
            return (False, f"Memory {memory_id} not found")
        proj.entries = [e for e in proj.entries if e.id != memory_id]
        self._rebuild_indexes(proj)
        self._dirty_projects.add(proj.project_path)
        self._save()
        return (True, f"Deleted memory {memory_id}")

    def batch_delete(
        self, project_path: str, memory_ids: Optional[list] = None, category: Optional[str] = None
    ) -> tuple[int, str]:
        """Delete by ids, or every entry of a category (rules and mistakes are protected)."""
        proj = self.get_project(project_path)
        if not proj:
            return (0, "Project not found")
        before = len(proj.entries)
        if memory_ids:
            id_set = set(memory_ids)
            proj.entries = [e for e in proj.entries if e.id not in id_set]
        elif category:
            if category in ("rule", "mistake"):
                return (
                    0,
                    f"Cannot bulk-delete '{category}' memories (protected). Use delete with specific memory_ids instead.",
                )
            proj.entries = [e for e in proj.entries if e.category != category]
        else:
            return (0, "Specify memory_ids or category")
        deleted = before - len(proj.entries)
        if deleted > 0:
            self._rebuild_indexes(proj)
            self._dirty_projects.add(proj.project_path)
            self._save()
        return (deleted, f"Deleted {deleted} memories")

    def promote_to_rule(self, project_path: str, memory_id: str, reason: Optional[str] = None) -> tuple[bool, str]:
        """Make a memory a rule."""
        proj = self.get_project(project_path)
        if not proj:
            return (False, "Project not found")
        entry = self._get_entry_by_id(proj, memory_id)
        if not entry:
            return (False, f"Memory {memory_id} not found")
        if entry.category == "rule":
            return (False, "Already a rule")
        entry.category = "rule"
        entry.relevance = max(entry.relevance, 8)
        if "rule" not in entry.tags:
            entry.tags.append("rule")
        if reason:
            entry.content = f"{entry.content} (Promoted to rule: {reason})"
        self._dirty_projects.add(proj.project_path)
        self._save()
        return (True, f"Promoted {memory_id} to rule")

    # ------------------------------------------------------------------
    # Injection scoring
    # ------------------------------------------------------------------

    def _score_memory_relevance(self, entry: MemoryEntry, context: dict) -> float:
        """An entry's relevance to a context ({"file_path", "tags", ...}):
        35% file match, 20% tag overlap, 20% recency, 15% importance, 10%
        access frequency, plus the category bonus (rule +0.3, mistake +0.2)."""
        score = 0.0
        file_score = 0.0
        ctx_file = context.get("file_path", "")
        if ctx_file:
            ctx_name = Path(ctx_file).name
            ctx_dir = str(Path(ctx_file).parent)
            ctx_ext = Path(ctx_file).suffix
            for rf in entry.related_files:
                rf_name = Path(rf).name
                if rf == ctx_file or rf_name == ctx_name:
                    file_score = 1.0
                    break
                elif str(Path(rf).parent) == ctx_dir:
                    file_score = max(file_score, 0.6)
                elif Path(rf).suffix == ctx_ext:
                    file_score = max(file_score, 0.2)
            if file_score < 0.4 and ctx_name in entry.content:
                file_score = max(file_score, 0.4)
        score += SCORE_WEIGHTS["file_match"] * file_score

        ctx_tags = set(context.get("tags", []) or [])
        if ctx_file:
            for pattern, tag in self.TAG_PATTERNS.items():
                if re.search(pattern, ctx_file, re.IGNORECASE):
                    ctx_tags.add(tag)
        tag_score = len(ctx_tags & set(entry.tags)) / len(ctx_tags) if ctx_tags and entry.tags else 0.0
        score += SCORE_WEIGHTS["tag_overlap"] * tag_score

        age_days = (time.time() - entry.last_accessed) / 86400
        score += SCORE_WEIGHTS["recency"] * math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)
        score += SCORE_WEIGHTS["relevance"] * (entry.relevance / 10.0)
        score += SCORE_WEIGHTS["access_freq"] * min(entry.access_count / 10.0, 1.0)
        # Category bonuses ride on top of the (<= 1.0) components, unclipped,
        # so a rule outranks a mistake on the same file.
        score += CATEGORY_BONUSES.get(entry.category, 0.0)
        return score

    def score_and_rank(
        self, project_path: str, context: dict, limit: int = 3, include_archive: bool = False
    ) -> list:
        """(entry, score) pairs for a context, best first."""
        proj = self.get_project(project_path)
        if not proj:
            return []
        entries = list(proj.entries)
        if include_archive:
            self._load_archive()
            archive_proj = self._archive_projects.get(self._normalize_path(project_path))
            if archive_proj:
                entries.extend(archive_proj.entries)
        scored = [(e, self._score_memory_relevance(e, context)) for e in entries]
        scored.sort(key=lambda x: x[1], reverse=True)
        for entry, _ in scored[:limit]:
            if entry.archived_at is None:
                entry.last_accessed = time.time()
                entry.access_count += 1
        if scored[:limit]:
            self._dirty_projects.add(proj.project_path)
            self._save()
        return scored[:limit]

    # ------------------------------------------------------------------
    # Embedding vectors (optional; the semantic tier)
    # ------------------------------------------------------------------

    def _save_project_embeddings(self, norm_path: str, ids: list, vecs: list):
        """Save a project's vectors (numpy binary or JSON), signature-stamped."""
        sig, _legacy = _embed_signature()
        pdir = self._project_dir(norm_path)
        if self._np is not None and vecs:
            matrix = self._np.array(vecs, dtype=self._np.float32)
            self._np.save(str(pdir / "embeddings.npy"), matrix)
            temp = (pdir / "embeddings_index.json").with_suffix(".json.tmp")
            temp.write_text(json.dumps({"ids": ids, "model": sig}), encoding="utf-8")
            temp.replace(pdir / "embeddings_index.json")
        elif vecs:
            emb_dict = {mid: vec for mid, vec in zip(ids, vecs)}
            temp = (pdir / "embeddings.json").with_suffix(".json.tmp")
            temp.write_text(json.dumps(emb_dict), encoding="utf-8")
            temp.replace(pdir / "embeddings.json")

    def _load_project_embeddings(self, norm_path: str) -> tuple:
        """(ids, matrix or dict) for a project; ([], None) when there are none,
        the data is corrupt, or another model built them."""
        sig, legacy = _embed_signature()
        pdir = self._project_dir(norm_path)
        if self._np is not None:
            npy_file = pdir / "embeddings.npy"
            index_file = pdir / "embeddings_index.json"
            if npy_file.exists() and index_file.exists():
                try:
                    idx_data = json.loads(index_file.read_text(encoding="utf-8"))
                    ids = idx_data.get("ids", [])
                    stamp = idx_data.get("model", legacy)
                    matrix = self._np.load(str(npy_file), mmap_mode="r")
                    if stamp == sig and len(matrix.shape) == 2 and matrix.shape[0] == len(ids):
                        return ids, matrix
                except Exception:
                    pass
        emb_file = pdir / "embeddings.json"
        if emb_file.exists():
            try:
                emb_dict = json.loads(emb_file.read_text(encoding="utf-8"))
                return list(emb_dict.keys()), emb_dict
            except Exception:
                pass
        return [], None

    def _project_has_embeddings(self, norm_path: str) -> bool:
        if norm_path not in self._manifest.get("projects", {}):
            return False
        pdir = self._projects_dir / self._manifest["projects"][norm_path]["hash"]
        if self._np is not None:
            return (pdir / "embeddings.npy").exists() and (pdir / "embeddings_index.json").exists()
        return (pdir / "embeddings.json").exists()

    def _load_embeddings(self):
        if self._embeddings_loaded:
            return
        if self._embeddings_file.exists():
            try:
                self._embeddings = json.loads(self._embeddings_file.read_text(encoding="utf-8"))
            except Exception:
                self._embeddings = {}
        self._embeddings_loaded = True

    def _save_embeddings(self):
        try:
            temp = self._embeddings_file.with_suffix(".json.tmp")
            temp.write_text(json.dumps(self._embeddings), encoding="utf-8")
            temp.replace(self._embeddings_file)
        except Exception:
            pass

    def _get_embedding(self, text: str) -> list:
        """A vector for ``text`` from the daemon's encoder; [] when there is none."""
        try:
            from windvane.daemon import embed_via_server

            return embed_via_server(text)
        except Exception:
            return []

    def _embed_batch(self, texts: list) -> list:
        """Vectors for many texts: the bulk worker for big jobs, the daemon otherwise."""
        from windvane.daemon import embed_batch_via_server

        embeddings = None
        try:
            from windvane.semantic.worker import bulk_threshold, embed_texts_bulk

            if len(texts) >= bulk_threshold():
                embeddings = embed_texts_bulk(texts)
        except Exception:
            embeddings = None
        if embeddings is None:
            embeddings = embed_batch_via_server(texts)
        return embeddings

    def _rerank(self, query: str, entries: list, limit: int = 10, project_path: str = "") -> list:
        """Entries reranked by vector similarity to ``query``; an entry with
        no vector scores 0.0 and keeps its place behind the scored ones."""
        query_emb = self._get_embedding(query)
        if not query_emb:
            return [(e, 1.0 - i * 0.01) for i, e in enumerate(entries[:limit])]
        emb_dict: dict = {}
        emb_matrix = None
        emb_ids: list = []
        if project_path:
            norm = self._normalize_path(project_path)
            if self._project_has_embeddings(norm):
                emb_ids, emb_data = self._load_project_embeddings(norm)
                if self._np is not None and hasattr(emb_data, "shape"):
                    emb_matrix = emb_data
                elif emb_ids:
                    emb_dict = emb_data if isinstance(emb_data, dict) else {}
        if self._np is not None and emb_matrix is not None and len(emb_ids) > 0:
            id_to_row = {mid: i for i, mid in enumerate(emb_ids)}
            query_arr = self._np.array(query_emb, dtype=self._np.float32)
            scored, unscored = [], []
            for entry in entries:
                if entry.id in id_to_row:
                    scored.append((entry, float(self._np.dot(emb_matrix[id_to_row[entry.id]], query_arr))))
                else:
                    unscored.append(entry)
            scored.sort(key=lambda x: x[1], reverse=True)
            result = scored[:limit]
            remaining = limit - len(result)
            if remaining > 0 and unscored:
                result.extend([(e, 0.0) for e in unscored[:remaining]])
            return result
        if not emb_dict:
            self._load_embeddings()
            emb_dict = self._embeddings
        scored, unscored = [], []
        for entry in entries:
            if entry.id in emb_dict:
                scored.append((entry, sum(a * b for a, b in zip(query_emb, emb_dict[entry.id]))))
            else:
                unscored.append(entry)
        scored.sort(key=lambda x: x[1], reverse=True)
        result = scored[:limit]
        remaining = limit - len(result)
        if remaining > 0 and unscored:
            result.extend([(e, 0.0) for e in unscored[:remaining]])
        return result

    def embed_memory(self, memory_id: str, content: str, project_path: str = ""):
        """Embed one entry into its project's pending file (the fast write a hook can afford)."""
        emb = self._get_embedding(content)
        if not emb:
            return
        if project_path:
            norm = self._normalize_path(project_path)
            if norm in self._manifest.get("projects", {}):
                pending_file = self._project_dir(norm) / "embeddings_pending.json"
                try:
                    raw = {}
                    if pending_file.exists():
                        raw = json.loads(pending_file.read_text(encoding="utf-8"))
                    sig, _legacy = _embed_signature()
                    vectors = raw.get("vectors", {}) if isinstance(raw, dict) and "vectors" in raw else dict(raw)
                    vectors[memory_id] = emb
                    temp = pending_file.with_suffix(".json.tmp")
                    temp.write_text(json.dumps({"model": sig, "vectors": vectors}), encoding="utf-8")
                    temp.replace(pending_file)
                    return
                except Exception:
                    pass
        self._load_embeddings()
        self._embeddings[memory_id] = emb
        self._save_embeddings()

    def vector_search(self, project_path: str, query: str, limit: int = 10) -> list:
        """(entry, similarity) pairs by vector alone, best first."""
        norm = self._normalize_path(project_path)
        if not self._project_has_embeddings(norm):
            return []
        query_emb = self._get_embedding(query)
        if not query_emb:
            return []
        proj = self.get_project(project_path)
        if not proj:
            return []
        ids, data = self._load_project_embeddings(norm)
        if not ids:
            return []
        entry_map = {e.id: e for e in proj.entries}
        if self._np is not None and hasattr(data, "shape"):
            query_arr = self._np.array(query_emb, dtype=self._np.float32)
            sims = self._np.dot(data, query_arr)
            if len(sims) > limit:
                top_idx = self._np.argpartition(sims, -limit)[-limit:]
                top_idx = top_idx[self._np.argsort(sims[top_idx])[::-1]]
            else:
                top_idx = self._np.argsort(sims)[::-1]
            return [(entry_map[ids[i]], float(sims[i])) for i in top_idx if ids[i] in entry_map]
        results = []
        for entry in proj.entries:
            if entry.id in data:
                results.append((entry, sum(a * b for a, b in zip(query_emb, data[entry.id]))))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def hybrid_search(
        self, project_path: str, query: str, file_path: str = "", tags: Optional[list] = None, limit: int = 10
    ) -> list:
        """Keyword, score and vector results fused by reciprocal rank, then
        reranked by vector; a zero score is no evidence and is dropped."""
        proj = self.get_project(project_path)
        if not proj:
            return []
        keyword_ids = [e.id for e in self.search_memories(project_path, query=query, tags=tags, limit=limit * 2)]
        scored_ids = [
            e.id for e, _ in self.score_and_rank(project_path, {"file_path": file_path, "tags": tags or []}, limit=limit * 2)
        ]
        vector_ids: list = []
        if self._project_has_embeddings(self._normalize_path(project_path)):
            vector_ids = [e.id for e, _ in self.vector_search(project_path, query, limit=limit * 2)]
        k = 60
        rrf_scores: dict = {}
        for rank, eid in enumerate(keyword_ids):
            rrf_scores[eid] = rrf_scores.get(eid, 0) + 1.5 / (k + rank + 1)
        for rank, eid in enumerate(scored_ids):
            rrf_scores[eid] = rrf_scores.get(eid, 0) + 1.0 / (k + rank + 1)
        for rank, eid in enumerate(vector_ids):
            rrf_scores[eid] = rrf_scores.get(eid, 0) + 0.5 / (k + rank + 1)
        entry_map = {e.id: e for e in proj.entries}
        ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        candidates = [entry_map[eid] for eid, _ in ranked[: limit * 2] if eid in entry_map]
        if candidates and query:
            reranked = self._rerank(query, candidates, limit, project_path=project_path)
            return [(e, s) for e, s in reranked if s > 0]
        return [(entry_map[eid], score) for eid, score in ranked[:limit] if eid in entry_map and score > 0]

    def pending_embedding_count(self, project_path: str) -> int:
        """How many memories lack a vector."""
        proj = self.get_project(project_path)
        if not proj:
            return 0
        try:
            existing_ids, existing_data = self._load_project_embeddings(self._normalize_path(project_path))
            existing_data = None  # drop a live mmap handle at once (Windows)
            existing_set = set(existing_ids)
        except Exception:
            existing_set = set()
        return sum(1 for e in proj.entries if e.id not in existing_set)

    def embed_all_memories(self, project_path: str, force: bool = False) -> int:
        """Embed every memory that has no vector yet (``force`` rebuilds all).
        Returns how many were embedded."""
        proj = self.get_project(project_path)
        if not proj:
            return 0
        norm = self._normalize_path(project_path)
        all_ids: list = []
        all_vecs: list = []
        if not force:
            existing_ids, existing_data = self._load_project_embeddings(norm)
            existing_set = set(existing_ids)
            if self._np is not None and hasattr(existing_data, "shape") and len(existing_data) > 0:
                all_ids = list(existing_ids)
                all_vecs = [existing_data[i].tolist() for i in range(len(existing_data))]
            elif isinstance(existing_data, dict):
                all_ids = list(existing_ids)
                all_vecs = [existing_data[mid] for mid in existing_ids]
            # The loader returns a LIVE mmap; on Windows np.save to the same
            # path fails while a handle is open. Rows are copied above.
            existing_data = None
        else:
            existing_set = set()
        pending_entries = [e for e in proj.entries if e.id not in existing_set]
        if not pending_entries:
            return 0
        count = 0
        try:
            embeddings = self._embed_batch([e.content[:500] for e in pending_entries])
            for entry, emb in zip(pending_entries, embeddings):
                if emb and (not all_vecs or len(emb) == len(all_vecs[0])):
                    all_ids.append(entry.id)
                    all_vecs.append(emb)
                    count += 1
        except Exception:
            count = 0
            for entry in pending_entries:
                emb = self._get_embedding(entry.content)
                if emb:
                    all_ids.append(entry.id)
                    all_vecs.append(emb)
                    count += 1
        if count > 0:
            self._save_project_embeddings(norm, all_ids, all_vecs)
        return count
