"""Manifest scanning for multi-file business context selections."""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_FILES = 500
DEFAULT_MAX_TOTAL_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_SINGLE_BYTES = 10 * 1024 * 1024
DEFAULT_RULES_VERSION = "context-rules-v1"

DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".cache",
        ".pytest_cache",
        "coverage",
        "tmp",
        "temp",
        ".mypy_cache",
        ".tox",
    }
)

BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff",
        ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
        ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv",
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
        ".exe", ".dll", ".msi", ".bin", ".so", ".dylib",
        ".db", ".sqlite", ".sqlite3", ".mdb",
    }
)

SUPPORTED_SUFFIXES = frozenset(
    {
        ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".html", ".css",
        ".sql", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".log",
        ".json", ".csv", ".pdf", ".docx", ".xlsx", ".pptx",
    }
)


@dataclass(frozen=True)
class ScanLimits:
    max_files: int = DEFAULT_MAX_FILES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_single_bytes: int = DEFAULT_MAX_SINGLE_BYTES


@dataclass(frozen=True)
class ManifestEntry:
    absolute_path: str
    relative_path: str
    suffix: str
    size_bytes: int
    status: str
    reason_code: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ContextManifest:
    root_paths: list[str]
    entries: list[ManifestEntry]
    limit_hit: str | None = None
    rules_version: str = DEFAULT_RULES_VERSION


def _safe_resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _entry_for_error(path: Path, reason_code: str, reason: str) -> ManifestEntry:
    return ManifestEntry(
        absolute_path=str(path),
        relative_path=path.name or str(path),
        suffix=path.suffix.lower(),
        size_bytes=0,
        status="failed",
        reason_code=reason_code,
        reason=reason,
    )


def _relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return relative or path.name


def scan_context_paths(
    paths: list[str | Path],
    *,
    limits: ScanLimits = ScanLimits(),
    excluded_directories: set[str] | None = None,
) -> ContextManifest:
    """Enumerate selected files without reading file bodies."""
    excluded = {name.casefold() for name in (excluded_directories or DEFAULT_EXCLUDED_DIRECTORIES)}
    roots: list[Path] = []
    seen_paths: set[str] = set()
    entries: list[ManifestEntry] = []
    candidates: list[tuple[Path, Path]] = []

    for raw_path in paths:
        path = _safe_resolve(raw_path)
        key = str(path).casefold()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        try:
            if not path.exists():
                entries.append(_entry_for_error(path, "path_not_found", f"文件不存在：{path}"))
                continue
            if path.is_dir():
                roots.append(path)
                for child in path.rglob("*"):
                    if child.is_file():
                        rel_parts = child.relative_to(path).parts
                        if any(part.casefold() in excluded for part in rel_parts[:-1]):
                            entries.append(
                                ManifestEntry(str(child), child.relative_to(path).as_posix(), child.suffix.lower(), child.stat().st_size, "excluded", "excluded_directory", "位于默认排除目录")
                            )
                        else:
                            candidates.append((path, child))
            elif path.is_file():
                roots.append(path.parent)
                candidates.append((path.parent, path))
            else:
                entries.append(_entry_for_error(path, "path_not_file", f"路径不是文件：{path}"))
        except (OSError, ValueError) as exc:
            entries.append(_entry_for_error(path, "stat_failed", f"无法读取路径信息：{exc}"))

    collision_counts: dict[str, int] = {}
    candidate_rels: list[tuple[Path, Path, str, int]] = []
    for root, path in candidates:
        try:
            size = path.stat().st_size
            rel = _relative_path(path, root)
        except OSError as exc:
            entries.append(_entry_for_error(path, "stat_failed", f"无法读取文件信息：{exc}"))
            continue
        collision_counts[rel.casefold()] = collision_counts.get(rel.casefold(), 0) + 1
        candidate_rels.append((root, path, rel, size))

    prepared: list[tuple[str, Path, str, int]] = []
    for root, path, rel, size in candidate_rels:
        if collision_counts[rel.casefold()] > 1:
            rel = f"{root.name}/{rel}" if root.name else rel
        prepared.append((rel, path, path.suffix.lower(), size))
    prepared.sort(key=lambda item: (item[0].casefold(), item[0]))

    included_files = 0
    total_bytes = 0
    limit_hit: str | None = None
    for rel, path, suffix, size in prepared:
        if suffix in BINARY_SUFFIXES:
            entries.append(ManifestEntry(str(path), rel, suffix, size, "excluded", "binary_file", "二进制文件不进入业务上下文"))
        elif suffix not in SUPPORTED_SUFFIXES:
            entries.append(ManifestEntry(str(path), rel, suffix, size, "excluded", "unsupported_format", "当前版本不支持此文件格式"))
        elif size > limits.max_single_bytes:
            entries.append(ManifestEntry(str(path), rel, suffix, size, "excluded", "single_file_too_large", f"单文件超过 {limits.max_single_bytes:,} 字节保护线"))
        elif included_files >= limits.max_files:
            limit_hit = limit_hit or "max_files"
            entries.append(ManifestEntry(str(path), rel, suffix, size, "excluded", "max_files", "达到文件数量保护线，等待手动选择"))
        elif total_bytes + size > limits.max_total_bytes:
            limit_hit = limit_hit or "max_total_bytes"
            entries.append(ManifestEntry(str(path), rel, suffix, size, "excluded", "max_total_bytes", "达到扫描总大小保护线，等待手动选择"))
        else:
            entries.append(ManifestEntry(str(path), rel, suffix, size, "included"))
            included_files += 1
            total_bytes += size

    entries.sort(key=lambda item: (item.relative_path.casefold(), item.relative_path, item.status))
    return ContextManifest([str(root) for root in roots], entries, limit_hit)
