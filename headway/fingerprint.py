"""Bounded filesystem and normalized tool-result fingerprints.

This module deliberately uses only pathlib and hashlib. It never invokes a
shell or git, follows no symlinks, and does not read outside the workspace.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

IGNORE_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".hermes"})
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")
CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s*(?:UTC|GMT|Z)?", re.IGNORECASE)
PID_RE = re.compile(r"\b(?:pid|process(?: id)?)\s*[:=#]?\s*\d+\b", re.IGNORECASE)
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec(?:ond)?s?|min(?:ute)?s?)\b", re.IGNORECASE)
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)


@dataclass(frozen=True)
class WorkspaceFingerprint:
    digest: str
    files: list[str]
    bytes_hashed: int
    partial: bool = False

    @property
    def short_id(self) -> str:
        return self.digest[:12]


@dataclass(frozen=True)
class EvidenceFingerprint:
    digest: str
    normalized: str
    timestamp_only: bool


class WorkspaceFingerprinter:
    """Hash relative paths and contents with explicit file/byte caps.

    Oversized files contribute their size plus bounded head/tail samples. When
    the total cap is reached, the sorted path listing still detects additions,
    removals and renames; ``partial`` makes this limit visible in status.
    """

    def __init__(self, root: str | Path, *, max_file_bytes: int = 1_000_000,
                 max_total_bytes: int = 20_000_000, max_files: int = 10_000,
                 touched_roots: Iterable[str | Path] | None = None):
        self.root = Path(root).expanduser().resolve()
        self.max_file_bytes = max(64, int(max_file_bytes))
        self.max_total_bytes = max(self.max_file_bytes, int(max_total_bytes))
        self.max_files = max(1, int(max_files))
        self.touched_roots = tuple(str(p) for p in (touched_roots or ()) )

    def capture(self) -> WorkspaceFingerprint:
        digest = hashlib.sha256()
        files: list[str] = []
        total = 0
        partial = False
        for path, rel in self._paths():
            if len(files) >= self.max_files:
                partial = True
                break
            files.append(rel)
            digest.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            try:
                size = path.stat().st_size
                digest.update(str(size).encode("ascii") + b"\0")
                remaining = self.max_total_bytes - total
                if remaining <= 0:
                    partial = True
                    continue
                if size > self.max_file_bytes:
                    partial = True
                content = self._sample(path, min(size, self.max_file_bytes, remaining))
                if size > len(content):
                    partial = True
                digest.update(content)
                total += len(content)
            except (OSError, ValueError) as exc:
                # Files can disappear or be replaced while a tool runs. The
                # exception type becomes observable without breaking execution.
                partial = True
                digest.update(type(exc).__name__.encode("ascii"))
            digest.update(b"\0")
        return WorkspaceFingerprint(digest.hexdigest(), files, total, partial)

    def _paths(self):
        roots = self.touched_roots or (".",)
        seen: set[str] = set()
        collected: list[tuple[Path, str]] = []
        for raw_root in roots:
            root = self._safe_root(raw_root)
            if root is None:
                continue
            if root.is_file() and not root.is_symlink():
                candidates = (root,)
            elif root.is_dir():
                candidates = self._walk(root)
            else:
                continue
            for path in candidates:
                try:
                    rel = path.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if rel not in seen:
                    seen.add(rel)
                    collected.append((path, rel))
        yield from sorted(collected, key=lambda item: item[1])

    def _safe_root(self, raw: str) -> Path | None:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            candidate = candidate.resolve()
            candidate.relative_to(self.root)
            return candidate
        except (OSError, ValueError):
            return None

    def _walk(self, root: Path):
        for current, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not (Path(current) / d).is_symlink())
            for name in sorted(names):
                path = Path(current) / name
                if path.is_file() and not path.is_symlink():
                    yield path

    @staticmethod
    def _sample(path: Path, limit: int) -> bytes:
        with path.open("rb") as stream:
            if limit < 256:
                return stream.read(limit)
            size = path.stat().st_size
            head = stream.read(limit // 2)
            stream.seek(max(0, size - (limit - len(head))))
            return head + b"\0HEADWAY-SAMPLE\0" + stream.read(limit - len(head))


def normalize_evidence(value, workspace: str | Path | None = None) -> str:
    """Remove volatile terminal noise without erasing meaningful numbers."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    if workspace:
        root = str(Path(workspace).expanduser().resolve())
        text = re.sub(re.escape(root) + r"(?:[\\/]+)?", "", text)
    text = UUID_RE.sub("<uuid>", text)
    text = ISO_DATE_RE.sub("<timestamp>", text)
    text = CLOCK_RE.sub("<timestamp>", text)
    text = PID_RE.sub("<pid>", text)
    text = DURATION_RE.sub("<duration>", text)
    return re.sub(r"\s+", " ", text).strip()


def evidence_fingerprint(value, workspace: str | Path | None = None) -> EvidenceFingerprint:
    normalized = normalize_evidence(value, workspace)
    volatile = re.sub(r"<(?:timestamp|pid|duration|uuid)>", "", normalized)
    words = re.findall(r"[a-z]+", volatile.lower())
    numbers = re.findall(r"\d+", volatile)
    poller_words = {"at", "watching", "waiting", "polling", "sleeping", "idle", "still",
                    "no", "progress", "heartbeat", "alive", "running", "check"}
    timestamp_only = bool(normalized) and not numbers and all(word in poller_words for word in words)
    digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
    return EvidenceFingerprint(digest, normalized, timestamp_only)
