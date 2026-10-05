"""
The cold tier: memories that went quiet, kept out of the hot path.

``archive.json`` beside the manifest holds, per project, the entries moved
out of the hot tier. Nothing here deletes a memory without a trace: an
archived entry stays searchable (``search_archive``) and restorable by id
(``restore_from_archive``).

- ``archive_old_memories``: entries untouched for ``archive_after_days``
  (``WINDVANE_ARCHIVE_DAYS``, default 14). Rules, mistakes and lessons never
  age out, nor does anything at ``ARCHIVE_EXEMPT_RELEVANCE`` or above.
- ``archive_stale_mistakes``: the background miner's hygiene pass for
  machine-written one-off mistakes (3+ weeks old, never recurred, away from
  current work).
- ``cleanup_memories``: near-duplicate removal (word Jaccard, and vector
  cosine when the semantic tier is on), relevance decay, and archiving
  before anything is dropped.
- ``consolidate_memories``: lists the tag groups large enough to merge.
  windvane writes no digest, so it never removes anything.
- ``get_clusters``: the clusters cleanup builds from shared tags.

``ArchiveMixin`` is mixed into ``windvane.store.MemoryStore``; its methods
use the store's own state and savers.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional


class ArchiveMixin:
    """The cold tier and the cleanup passes of ``MemoryStore``."""

    # ------------------------------------------------------------------
    # archive.json
    # ------------------------------------------------------------------

    def _load_archive(self):
        """Lazy-load the archive. Only archive operations call this."""
        if self._archive_loaded:
            return
        from windvane.store import ProjectMemory

        if self.archive_file.exists():
            try:
                data = json.loads(self.archive_file.read_text(encoding="utf-8"))
                for path, proj_data in data.get("projects", {}).items():
                    norm_path = self._normalize_path(path)
                    proj_data["project_path"] = norm_path
                    proj_data.setdefault("project_name", Path(norm_path).name)
                    self._archive_projects[norm_path] = ProjectMemory.from_dict(proj_data)
            except Exception:
                pass  # the archive is best-effort
        self._archive_loaded = True

    def _save_archive(self) -> bool:
        data = {
            "version": 2,
            "projects": {path: proj.model_dump() for path, proj in self._archive_projects.items()},
        }
        try:
            temp_file = self.archive_file.with_suffix(".json.tmp")
            temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp_file.replace(self.archive_file)
            return True
        except Exception:
            return False

    def _archive_project_for(self, norm_path: str, project_name: str):
        from windvane.store import ProjectMemory

        self._load_archive()
        if norm_path not in self._archive_projects:
            self._archive_projects[norm_path] = ProjectMemory(project_path=norm_path, project_name=project_name)
        return self._archive_projects[norm_path]

    def _is_archivable(self, entry) -> bool:
        """Should this entry move to the archive by age?"""
        from windvane.store import ARCHIVE_EXEMPT_RELEVANCE

        # Rules, mistakes and lessons never age out (lessons live in their
        # source file; mistake hygiene has its own narrow gate).
        if entry.category in ("rule", "mistake", "lesson"):
            return False
        if entry.relevance >= ARCHIVE_EXEMPT_RELEVANCE:
            return False
        return (time.time() - entry.last_accessed) / 86400 > self.archive_after_days

    def archive_old_memories(self, project_path: str, dry_run: bool = True) -> dict:
        """Move old inactive memories from the hot tier to the archive."""
        proj = self.get_project(project_path)
        if not proj:
            return {"archived_count": 0, "entries": [], "dry_run": dry_run}
        to_archive = [e for e in proj.entries if self._is_archivable(e)]
        report = {
            "archived_count": len(to_archive),
            "entries": [
                {
                    "id": e.id,
                    "category": e.category,
                    "age_days": int((time.time() - e.last_accessed) / 86400),
                    "preview": e.content[:60] + "..." if len(e.content) > 60 else e.content,
                }
                for e in to_archive
            ],
            "dry_run": dry_run,
        }
        if not dry_run and to_archive:
            archive_proj = self._archive_project_for(self._normalize_path(project_path), proj.project_name)
            archive_ids = {e.id for e in archive_proj.entries}
            for entry in to_archive:
                entry.archived_at = time.time()
                if entry.id not in archive_ids:
                    archive_proj.entries.append(entry)
            archived_ids = {e.id for e in to_archive}
            proj.entries = [e for e in proj.entries if e.id not in archived_ids]
            self._rebuild_indexes(proj)
            self._dirty_projects.add(proj.project_path)
            self._save()
            self._save_archive()
        return report

    def _move_entries_to_archive(self, proj, entries: list) -> int:
        """Move entries from a hot project into the archive (no save)."""
        archive_proj = self._archive_project_for(self._normalize_path(proj.project_path), proj.project_name)
        archive_ids = {e.id for e in archive_proj.entries}
        moved = 0
        for entry in entries:
            entry.archived_at = time.time()
            if entry.id not in archive_ids:
                archive_proj.entries.append(entry)
                moved += 1
        gone = {e.id for e in entries}
        proj.entries = [e for e in proj.entries if e.id not in gone]
        self._rebuild_indexes(proj)
        self._dirty_projects.add(proj.project_path)
        return moved

    def archive_memory(self, project_path: str, memory_id: str) -> tuple[bool, str]:
        """Move one active memory to the archive by its id."""
        proj = self.get_project(project_path)
        entry = self._get_entry_by_id(proj, memory_id) if proj else None
        if not proj or not entry:
            return (False, f"Memory {memory_id} not found in this project")
        self._move_entries_to_archive(proj, [entry])
        self._save()
        self._save_archive()
        return (True, f"Archived memory {memory_id}; memory(restore, memory_id='{memory_id}') brings it back")

    def _recurring_error_sigs(self, project_path: str) -> list:
        """(normalized class, template) pairs from the mined patterns.json,
        walking ancestors (mining pools at the workspace root)."""
        sigs = []
        try:
            norm = self._normalize_path(project_path)
            for _ in range(8):
                if norm in self._manifest.get("projects", {}):
                    p = self._project_dir(norm) / "patterns.json"
                    if p.exists():
                        data = json.loads(p.read_text(encoding="utf-8"))
                        for e in data.get("recurring_errors", []):
                            label, _, tmpl = str(e.get("message_pattern", "")).partition(": ")
                            sigs.append((re.sub(r"[^a-z]", "", label.lower()), tmpl))
                        break
                parent = self._normalize_path(str(Path(norm).parent))
                if parent == norm:
                    break
                norm = parent
        except Exception:
            pass
        return sigs

    def archive_stale_mistakes(
        self,
        project_path: str,
        recent_files: "Optional[set]" = None,
        dry_run: bool = True,
        min_age_days: int = 21,
    ) -> dict:
        """Archive machine-written mistakes that never recurred.

        Mistakes are normally archive-protected: every one shows in pre-edit
        banners for good. Machine-written ones are not written with intent,
        and a one-off typo from weeks ago is banner noise. One is archived
        (never deleted) only when ALL hold:
        - a machine source (auto-detected, session_mining, or none; a mistake
          logged by hand and every rule are untouched);
        - created and last accessed over ``min_age_days`` ago;
        - its error signature is NOT among the mined recurring errors;
        - none of its related files was edited recently (``recent_files``
          basenames).
        Archived entries stay searchable and restorable."""
        proj = self.get_project(project_path)
        if not proj:
            return {"archived_count": 0, "entries": [], "dry_run": dry_run}

        try:
            from windvane.mining.patterns import _normalize_error_msg
        except Exception:
            def _normalize_error_msg(msg: str) -> str:  # the miner is not installed
                return msg

        recurring = self._recurring_error_sigs(project_path)
        recent = {Path(f).name.lower() for f in (recent_files or set())}
        now = time.time()
        cutoff = min_age_days * 86400

        def _is_recurring(desc: str) -> bool:
            label, sep, msg = desc.partition(": ")
            ncls = re.sub(r"[^a-z]", "", label.lower())
            tmpl = _normalize_error_msg(msg if sep else desc)
            for rcls, rtmpl in recurring:
                if rcls != ncls:
                    continue
                k = min(len(tmpl), len(rtmpl))
                if k >= 15 and tmpl[:k] == rtmpl[:k]:
                    return True
            return False

        to_archive = []
        machine = ("", "auto-detected", "session_mining")
        for e in proj.entries:
            if e.category != "mistake" or e.archived_at is not None:
                continue
            if (e.source or "") not in machine:
                continue
            if now - e.created_at < cutoff or now - e.last_accessed < cutoff:
                continue
            if recent and any(Path(f).name.lower() in recent for f in e.related_files):
                continue
            desc = e.content[9:] if e.content.startswith("MISTAKE: ") else e.content
            desc = desc.split(" - Fix: ", 1)[0]
            if _is_recurring(desc):
                continue
            to_archive.append(e)

        report = {
            "archived_count": len(to_archive),
            "entries": [
                {"id": e.id, "age_days": int((now - e.created_at) / 86400), "preview": e.content[:60]}
                for e in to_archive
            ],
            "dry_run": dry_run,
        }
        if not dry_run and to_archive:
            self._move_entries_to_archive(proj, to_archive)
            self._save()
            self._save_archive()
        return report

    def restore_from_archive(self, project_path: str, memory_id: str) -> tuple[bool, str]:
        """Move a memory from the archive back to the hot tier."""
        self._load_archive()
        archive_proj = self._archive_projects.get(self._normalize_path(project_path))
        if not archive_proj:
            return (False, "No archive for this project")
        entry = self._get_entry_by_id(archive_proj, memory_id)
        if not entry:
            return (False, f"Memory {memory_id} not found in archive")
        entry.archived_at = None
        entry.last_accessed = time.time()
        entry.access_count += 1
        proj = self.remember_project(project_path)
        proj.entries.append(entry)
        self._update_indexes(proj, entry)
        archive_proj.entries = [e for e in archive_proj.entries if e.id != memory_id]
        self._dirty_projects.add(proj.project_path)
        self._save()
        self._save_archive()
        return (True, f"Restored memory {memory_id} to active")

    def search_archive(
        self, project_path: str, query: Optional[str] = None, tags: Optional[list] = None, limit: int = 5
    ) -> list:
        """Archived memories by keyword or tag (all of them with neither).
        Read-only: access counts are not touched."""
        self._load_archive()
        archive_proj = self._archive_projects.get(self._normalize_path(project_path))
        if not archive_proj:
            return []
        results = []
        for entry in archive_proj.entries:
            matched = False
            if query and set(query.lower().split()) & set(entry.content.lower().split()):
                matched = True
            if tags and set(tags) & set(entry.tags):
                matched = True
            if not query and not tags:
                matched = True
            if matched:
                results.append(entry)
        return sorted(results, key=lambda x: x.relevance, reverse=True)[:limit]

    def get_archive_stats(self, project_path: str) -> dict:
        """Hot vs archived counts, by category."""
        self._load_archive()
        hot_proj = self.get_project(project_path)
        archive_proj = self._archive_projects.get(self._normalize_path(project_path))
        hot_entries = hot_proj.entries if hot_proj else []
        archive_entries = archive_proj.entries if archive_proj else []
        hot_cats: dict = {}
        for e in hot_entries:
            hot_cats[e.category] = hot_cats.get(e.category, 0) + 1
        archive_cats: dict = {}
        for e in archive_entries:
            archive_cats[e.category] = archive_cats.get(e.category, 0) + 1
        return {
            "hot_total": len(hot_entries),
            "hot_categories": hot_cats,
            "archive_total": len(archive_entries),
            "archive_categories": archive_cats,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_memories(
        self,
        project_path: str,
        dry_run: bool = True,
        min_relevance: int = 3,
        max_age_days: int = 30,
        apply_decay: bool = True,
    ) -> dict:
        """Clean up a project's memories:

        1. broken or incomplete entries are removed;
        2. near-duplicates (word Jaccard at 0.85, and vector cosine at 0.85
           when the semantic tier is on) are merged into the original;
        3. old inactive entries move to the archive (before any decay);
        4. old low-relevance entries decay, and drop below ``min_relevance``;
        5. tags shared by three or more entries become clusters.

        Rules, mistakes and lessons never decay; lessons are left alone
        entirely (their file is the source of truth). ``dry_run`` reports
        without changing anything."""
        proj = self.get_project(project_path)
        if not proj:
            return {"error": "Project not found"}

        report: dict = {
            "broken_found": [],
            "duplicates_found": [],
            "duplicates_merged": [],
            "archived": [],
            "decayed": [],
            "removed": [],
            "clusters_created": [],
            "dry_run": dry_run,
            "total_memories": len(proj.entries),
        }

        for entry in proj.entries:
            if entry.category in ("lesson", "rule", "mistake"):
                # A rule or a mistake is a deliberate record, and a short one
                # ("Use pathlib.") is a rule, not a broken memory.
                continue
            reason = self._is_broken_memory(entry.content)
            if reason:
                report["broken_found"].append(
                    {
                        "entry_id": entry.id,
                        "reason": reason,
                        "content_preview": entry.content[:60] + "..." if len(entry.content) > 60 else entry.content,
                    }
                )
                report["removed"].append({"entry_id": entry.id, "reason": f"Broken memory: {reason}"})

        removed_ids = {r["entry_id"] for r in report["removed"]}
        seen_content: dict = {}
        for entry in proj.entries:
            if entry.id in removed_ids or entry.category == "lesson":
                continue
            dup = self._is_duplicate(entry.content, list(seen_content.values()), threshold=0.85)
            if dup:
                report["duplicates_found"].append(
                    {"entry_id": entry.id, "duplicate_of": dup.id, "content_preview": entry.content[:50] + "..."}
                )
            else:
                seen_content[entry.id] = entry

        # Vector dedupe: catches the same memory reworded, which Jaccard misses.
        try:
            np = self._np
            if np is not None:
                dup_ids = {d["entry_id"] for d in report["duplicates_found"]}
                candidates = [
                    e for e in proj.entries if e.id not in removed_ids and e.id not in dup_ids and e.category != "lesson"
                ]
                if len(candidates) >= 2:
                    embeddings = self._embed_batch([e.content for e in candidates])
                    valid_embs = [(i, emb) for i, emb in enumerate(embeddings) if emb and len(emb) > 0]
                    if len(valid_embs) >= 2:
                        indices = [i for i, _ in valid_embs]
                        matrix = np.array([emb for _, emb in valid_embs], dtype=np.float32)
                        sims = np.dot(matrix, matrix.T)
                        semantic_keep = set(range(len(indices)))
                        for a in range(len(sims)):
                            if a not in semantic_keep:
                                continue
                            for b in range(a + 1, len(sims)):
                                if b not in semantic_keep:
                                    continue
                                if sims[a][b] > 0.85:
                                    orig, dupe = candidates[indices[a]], candidates[indices[b]]
                                    report["duplicates_found"].append(
                                        {
                                            "entry_id": dupe.id,
                                            "duplicate_of": orig.id,
                                            "content_preview": dupe.content[:50] + "...",
                                            "method": "semantic",
                                        }
                                    )
                                    semantic_keep.discard(b)
        except Exception:
            pass  # no encoder: Jaccard alone

        if apply_decay:
            removed_now = {r["entry_id"] for r in report["removed"]}
            dup_now = {d["entry_id"] for d in report["duplicates_found"]}
            for entry in proj.entries:
                if entry.id in removed_now or entry.id in dup_now:
                    continue
                if self._is_archivable(entry):
                    report["archived"].append(
                        {
                            "entry_id": entry.id,
                            "age_days": int((time.time() - entry.last_accessed) / 86400),
                            "content_preview": entry.content[:50] + "...",
                        }
                    )

        protected_categories = {"rule", "mistake", "lesson"}
        if apply_decay:
            now = time.time()
            for entry in proj.entries:
                if any(r["entry_id"] == entry.id for r in report["removed"]):
                    continue
                if entry.category in protected_categories:
                    continue
                age_days = (now - entry.last_accessed) / 86400
                if age_days > max_age_days and entry.relevance < 7:
                    decay_amount = int((age_days - max_age_days) / 7)  # -1 per week over the threshold
                    new_relevance = max(1, entry.relevance - decay_amount)
                    if new_relevance < entry.relevance:
                        report["decayed"].append(
                            {
                                "entry_id": entry.id,
                                "old_relevance": entry.relevance,
                                "new_relevance": new_relevance,
                                "age_days": int(age_days),
                                "content_preview": entry.content[:50] + "...",
                            }
                        )
                        if new_relevance < min_relevance:
                            report["removed"].append(
                                {
                                    "entry_id": entry.id,
                                    "reason": f"Relevance decayed to {new_relevance} (below {min_relevance})",
                                }
                            )

        tag_groups: dict = defaultdict(list)
        for entry in proj.entries:
            for tag in entry.tags:
                tag_groups[tag].append(entry.id)
        for tag, entry_ids in tag_groups.items():
            if len(entry_ids) >= 3:
                name = f"{tag.title()} Memories"
                if not any(c.name == name for c in proj.clusters.values()):
                    report["clusters_created"].append({"name": name, "tag": tag, "memory_count": len(entry_ids)})

        actions = []
        if report["broken_found"]:
            actions.append(f"{len(report['broken_found'])} broken")
        if report["duplicates_found"]:
            actions.append(f"{len(report['duplicates_found'])} duplicates")
        if report["archived"]:
            actions.append(f"{len(report['archived'])} archived")
        if report["decayed"]:
            actions.append(f"{len(report['decayed'])} decayed")
        if report["clusters_created"]:
            actions.append(f"{len(report['clusters_created'])} new clusters")
        if actions:
            action_word = "would be cleaned" if dry_run else "cleaned"
            report["summary"] = f"Found: {', '.join(actions)}. {len(report['removed'])} entries {action_word}."
        else:
            report["summary"] = f"All {len(proj.entries)} memories are clean. No action needed."

        if not dry_run:
            self._apply_cleanup(proj, report)
            proj.last_cleanup = time.time()
            self._dirty_projects.add(proj.project_path)
            self._save()
        return report

    def _is_broken_memory(self, content: str) -> Optional[str]:
        """Why a memory is broken or incomplete, or None when it is fine."""
        if not content or not content.strip():
            return "Empty content"
        if len(content.strip()) < 20:
            return "Too short (< 20 chars)"
        for pattern in ("...\n##", ". Key finding: \n", "Key finding: \n##", ": \n##"):
            if pattern in content:
                return f"Truncated content (contains '{pattern.strip()}')"
        stripped = content.rstrip()
        if stripped and stripped[-1] not in ".!?\"')]:;":
            last_line = stripped.split("\n")[-1]
            if len(last_line) > 10 and " " in last_line[-20:]:
                words = content.split()
                if len(words) > 5:
                    last_word = words[-1] if words else ""
                    if last_word and not last_word[-1].isalnum():
                        pass
                    elif len(last_word) == 1 and last_word.isalpha():
                        return "Truncated (ends with single letter)"
        for pattern in ("TODO:", "FIXME:", "...", "[placeholder]", "[TBD]"):
            if content.strip() == pattern or content.strip().endswith(pattern):
                return f"Placeholder content ({pattern})"
        return None

    def _apply_cleanup(self, proj, report: dict):
        """Carry out a cleanup report."""
        from windvane.store import MemoryCluster

        ids_to_remove = set()
        for dup in report["duplicates_found"]:
            entry = self._get_entry_by_id(proj, dup["entry_id"])
            original = self._get_entry_by_id(proj, dup["duplicate_of"])
            if entry and original:
                original.tags = list(set(original.tags + entry.tags))
                original.related_files = list(set(original.related_files + entry.related_files))
                original.access_count += entry.access_count
                if entry.relevance > original.relevance:
                    original.relevance = entry.relevance
                    original.content = entry.content
                ids_to_remove.add(entry.id)
                report["duplicates_merged"].append(dup["entry_id"])

        if report.get("archived"):
            archive_proj = self._archive_project_for(self._normalize_path(proj.project_path), proj.project_name)
            archive_ids = {e.id for e in archive_proj.entries}
            for arch_info in report["archived"]:
                entry = self._get_entry_by_id(proj, arch_info["entry_id"])
                if entry and entry.id not in archive_ids:
                    entry.archived_at = time.time()
                    archive_proj.entries.append(entry)
                    ids_to_remove.add(entry.id)
            self._save_archive()

        for decay_info in report["decayed"]:
            entry = self._get_entry_by_id(proj, decay_info["entry_id"])
            if entry:
                entry.relevance = decay_info["new_relevance"]
        for removal in report["removed"]:
            ids_to_remove.add(removal["entry_id"])
        proj.entries = [e for e in proj.entries if e.id not in ids_to_remove]
        self._rebuild_indexes(proj)

        for cluster_info in report["clusters_created"]:
            tag = cluster_info["tag"]
            cluster_id = f"cluster_{tag}_{int(time.time())}"
            entry_ids = [e.id for e in proj.entries if tag in e.tags]
            contents = [e.content[:100] for e in proj.entries if e.id in entry_ids][:5]
            summary = f"Memories about {tag}: " + "; ".join(contents)
            proj.clusters[cluster_id] = MemoryCluster(
                cluster_id=cluster_id,
                name=cluster_info["name"],
                memory_ids=entry_ids,
                summary=summary[:200],
                tags=[tag],
                relevance=5,
            )
            for entry in proj.entries:
                if entry.id in entry_ids:
                    entry.cluster_id = cluster_id

    # ------------------------------------------------------------------
    # Clusters and consolidation (listing only)
    # ------------------------------------------------------------------

    def get_clusters(self, project_path: str, cluster_id: Optional[str] = None) -> dict:
        """A project's clusters, or one cluster with its memories."""
        proj = self.get_project(project_path)
        if not proj:
            return {"error": "Project not found", "clusters": []}
        if cluster_id:
            cluster = proj.clusters.get(cluster_id)
            if not cluster:
                return {"error": f"Cluster {cluster_id} not found", "clusters": []}
            memories = [m for m in (self._get_entry_by_id(proj, mid) for mid in cluster.memory_ids) if m]
            return {
                "cluster": {
                    "id": cluster.cluster_id,
                    "name": cluster.name,
                    "summary": cluster.summary,
                    "tags": cluster.tags,
                    "memory_count": len(memories),
                    "memories": [
                        {"id": m.id, "content": m.content, "relevance": m.relevance, "tags": m.tags}
                        for m in sorted(memories, key=lambda x: x.relevance, reverse=True)
                    ],
                }
            }
        clusters = [
            {
                "id": c.cluster_id,
                "name": c.name,
                "summary": c.summary,
                "tags": c.tags,
                "memory_count": len(c.memory_ids),
                "relevance": c.relevance,
            }
            for c in proj.clusters.values()
        ]
        clustered_ids = set()
        for c in proj.clusters.values():
            clustered_ids.update(c.memory_ids)
        return {
            "clusters": sorted(clusters, key=lambda x: x["relevance"], reverse=True),
            "unclustered_count": len([e for e in proj.entries if e.id not in clustered_ids]),
            "total_memories": len(proj.entries),
        }

    def consolidate_memories(self, project_path: str, tag: Optional[str] = None, dry_run: bool = True) -> dict:
        """The tag groups worth consolidating: ten or more entries sharing a
        tag, rules and mistakes never included (summarizing a specific,
        actionable error into a blob destroys it). windvane writes no
        digest, so this lists the groups and removes nothing, whatever
        ``dry_run`` says; merging a group is a person's edit (modify, delete,
        archive)."""
        proj = self.get_project(project_path)
        if not proj:
            return {"error": "Project not found"}
        report: dict = {
            "groups_found": [],
            "consolidated": [],
            "dry_run": dry_run,
            "original_count": len(proj.entries),
        }
        tag_groups: dict = {}
        for entry in proj.entries:
            if entry.category in ("rule", "mistake"):
                continue
            for t in entry.tags:
                if tag and t != tag:
                    continue
                tag_groups.setdefault(t, []).append(entry)
        for t, entries in tag_groups.items():
            if len(entries) < 10:
                continue
            report["groups_found"].append(
                {
                    "tag": t,
                    "count": len(entries),
                    "entries": [
                        {"id": e.id, "preview": e.content[:60] + "..." if len(e.content) > 60 else e.content}
                        for e in entries[:5]
                    ],
                }
            )
        if report["groups_found"]:
            report["summary"] = (
                f"Found {len(report['groups_found'])} groups that could be consolidated "
                "(listed only; nothing was merged)"
            )
        else:
            report["summary"] = "No groups found that need consolidation"
        return report
