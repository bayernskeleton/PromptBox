from promptbox_mvp.context_manifest import (
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_SINGLE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    ScanLimits,
    scan_context_paths,
)


def test_folder_scan_recurses_and_returns_relative_paths(tmp_path):
    (tmp_path / "README.md").write_text("说明", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)", encoding="utf-8")

    result = scan_context_paths([tmp_path])

    assert [item.relative_path for item in result.entries if item.status == "included"] == [
        "README.md",
        "src/main.py",
    ]
    assert result.root_paths == [str(tmp_path)]


def test_scan_marks_default_excluded_directory_visible(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.js").write_text("x", encoding="utf-8")

    result = scan_context_paths([tmp_path])

    excluded = [item for item in result.entries if item.status == "excluded"]
    assert excluded
    assert excluded[0].relative_path.startswith("node_modules/")
    assert excluded[0].reason_code == "excluded_directory"


def test_scan_stops_at_file_count_limit_and_keeps_manifest(tmp_path):
    for index in range(3):
        (tmp_path / f"f{index}.txt").write_text(str(index), encoding="utf-8")

    result = scan_context_paths([tmp_path], limits=ScanLimits(max_files=2))

    assert len(result.entries) == 3
    assert result.limit_hit == "max_files"
    assert sum(item.status == "included" for item in result.entries) == 2


def test_scan_marks_oversized_file_without_reading_it(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * 11)

    result = scan_context_paths(
        [tmp_path], limits=ScanLimits(max_single_bytes=10)
    )

    item = result.entries[0]
    assert item.status == "excluded"
    assert item.reason_code == "single_file_too_large"
    assert item.size_bytes == 11


def test_scan_rejects_duplicate_paths_from_multiselect(tmp_path):
    path = tmp_path / "same.md"
    path.write_text("x", encoding="utf-8")

    result = scan_context_paths([path, path])

    assert len(result.entries) == 1


def test_default_limits_match_product_protection_line():
    assert DEFAULT_MAX_FILES == 500
    assert DEFAULT_MAX_TOTAL_BYTES == 100 * 1024 * 1024
    assert DEFAULT_MAX_SINGLE_BYTES == 10 * 1024 * 1024


def test_scan_marks_binary_file_visible(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"PNG")

    result = scan_context_paths([path])

    assert result.entries[0].status == "excluded"
    assert result.entries[0].reason_code == "binary_file"


def test_scan_marks_total_size_limit_without_dropping_manifest(tmp_path):
    for index in range(2):
        (tmp_path / f"f{index}.txt").write_bytes(b"12345")

    result = scan_context_paths(
        [tmp_path], limits=ScanLimits(max_total_bytes=5)
    )

    assert len(result.entries) == 2
    assert result.limit_hit == "max_total_bytes"
    assert sum(item.status == "included" for item in result.entries) == 1
    assert result.entries[1].reason_code == "max_total_bytes"


def test_scan_marks_missing_path_as_failed(tmp_path):
    result = scan_context_paths([tmp_path / "gone.md"])

    assert result.entries[0].status == "failed"
    assert result.entries[0].reason_code == "path_not_found"


def test_multi_root_collision_gets_root_prefix(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "README.md").write_text("one", encoding="utf-8")
    (second / "README.md").write_text("two", encoding="utf-8")

    result = scan_context_paths([first, second])

    included = [item.relative_path for item in result.entries if item.status == "included"]
    assert included == ["one/README.md", "two/README.md"]
