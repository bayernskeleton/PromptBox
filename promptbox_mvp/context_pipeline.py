"""Shared local pipeline for selecting, parsing, and assembling context."""

from pathlib import Path

from .context_manifest import ContextManifest, ManifestEntry, scan_context_paths
from .context_pack import ContextPack, assemble_context_pack
from .context_parsers import ParserResult, parse_context_file


def build_context_from_selection(
    paths: list[str | Path],
    *,
    root_paths: list[str | Path] | None = None,
) -> tuple[ContextManifest, ContextPack]:
    """Scan and parse selected paths, then assemble one deterministic Pack."""
    manifest = scan_context_paths(paths)
    parsed: list[tuple[ManifestEntry, ParserResult]] = []
    failed: list[ManifestEntry] = []
    for entry in manifest.entries:
        if entry.status != "included":
            if entry.status in {"failed", "excluded"} and entry.reason_code:
                failed.append(entry)
            continue
        result = parse_context_file(entry.absolute_path)
        if result.status == "success":
            parsed.append((entry, result))
        else:
            failed.append(
                ManifestEntry(
                    absolute_path=entry.absolute_path,
                    relative_path=entry.relative_path,
                    suffix=entry.suffix,
                    size_bytes=entry.size_bytes,
                    status="failed",
                    reason_code=result.reason_code,
                    reason=result.reason,
                )
            )
    pack = assemble_context_pack(parsed, failed_entries=failed)
    if root_paths is not None:
        manifest = ContextManifest(
            root_paths=[str(Path(path).expanduser().resolve(strict=False)) for path in root_paths],
            entries=manifest.entries,
            limit_hit=manifest.limit_hit,
            rules_version=manifest.rules_version,
        )
    return manifest, pack
