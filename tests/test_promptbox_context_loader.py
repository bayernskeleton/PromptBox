"""P1 上下文来源解析测试：标准库实现，零新增依赖。

覆盖：文本直读、JSON 规范化、CSV 转 Markdown 表格、编码回退、不支持格式。
"""

import json

import pytest

from promptbox_mvp.context_loader import (
    SUPPORTED_TEXT_SUFFIXES,
    load_context_file,
    read_text_with_fallback,
)


def write(tmp_path, name, content, encoding="utf-8"):
    path = tmp_path / name
    path.write_text(content, encoding=encoding)
    return path


# ── 纯文本类 ──────────────────────────────────────────────────


def test_loads_plain_text_file(tmp_path):
    path = write(tmp_path, "note.txt", "第一行\n第二行")

    result = load_context_file(str(path))

    assert result["text"] == "第一行\n第二行"
    assert result["label"] == "note.txt"
    assert result["chars"] == len("第一行\n第二行")
    assert result["truncated"] is False


def test_loads_markdown_file(tmp_path):
    path = write(tmp_path, "doc.md", "# 标题\n\n正文")

    assert load_context_file(str(path))["text"] == "# 标题\n\n正文"


def test_loads_python_source(tmp_path):
    path = write(tmp_path, "mod.py", "def f():\n    return 1\n")

    assert "def f():" in load_context_file(str(path))["text"]


def test_supported_text_suffixes_cover_expected_formats():
    for suffix in (".txt", ".md", ".py", ".log", ".yaml", ".yml"):
        assert suffix in SUPPORTED_TEXT_SUFFIXES


# ── JSON 规范化 ───────────────────────────────────────────────


def test_json_is_pretty_printed_without_ascii_escaping(tmp_path):
    path = write(tmp_path, "data.json", '{"名称":"数云","值":[1,2]}')

    text = load_context_file(str(path))["text"]

    assert "数云" in text
    assert "\\u" not in text
    assert text == json.dumps(
        {"名称": "数云", "值": [1, 2]}, indent=2, ensure_ascii=False
    )


def test_malformed_json_is_rejected_with_structured_reason(tmp_path):
    """坏 JSON 不得作为已解析正文送入新上下文链路。"""
    path = write(tmp_path, "broken.json", "{不是合法 JSON")

    with pytest.raises(ValueError, match="JSON 解析失败"):
        load_context_file(str(path))


# ── CSV → Markdown 表格 ───────────────────────────────────────


def test_csv_becomes_aligned_markdown_table(tmp_path):
    path = write(tmp_path, "t.csv", "姓名,城市\n张三,上海\n李四,北京\n")

    text = load_context_file(str(path))["text"]
    lines = text.strip().split("\n")

    assert lines[0] == "| 姓名 | 城市 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 张三 | 上海 |"
    assert lines[3] == "| 李四 | 北京 |"


def test_csv_escapes_pipe_characters(tmp_path):
    path = write(tmp_path, "p.csv", "a,b\nx|y,z\n")

    text = load_context_file(str(path))["text"]

    assert r"x\|y" in text


def test_csv_row_limit_truncates_and_reports_total(tmp_path):
    rows = "\n".join(f"r{i},v{i}" for i in range(300))
    path = write(tmp_path, "big.csv", "c1,c2\n" + rows + "\n")

    result = load_context_file(str(path), csv_row_limit=200)

    assert result["truncated"] is True
    assert "300" in result["note"]
    assert result["text"].count("\n") <= 202


def test_empty_csv_returns_empty_text(tmp_path):
    path = write(tmp_path, "empty.csv", "")

    assert load_context_file(str(path))["text"] == ""


# ── 编码回退（Windows 必需）─────────────────────────────────────


def test_reads_utf8_bom_file(tmp_path):
    path = write(tmp_path, "bom.txt", "带 BOM 的内容", encoding="utf-8-sig")

    text = load_context_file(str(path))["text"]

    assert text == "带 BOM 的内容"
    assert not text.startswith("\ufeff")


def test_reads_gbk_file_via_fallback(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("中文内容测试".encode("gbk"))

    assert load_context_file(str(path))["text"] == "中文内容测试"


def test_read_text_with_fallback_reports_encoding_used(tmp_path):
    path = tmp_path / "g.txt"
    path.write_bytes("内容".encode("gbk"))

    text, encoding = read_text_with_fallback(path)

    assert text == "内容"
    assert encoding == "gbk"


def test_undecodable_file_raises_with_path_in_message(tmp_path):
    path = tmp_path / "bin.dat"
    path.write_bytes(b"\xff\xfe\x00\x81\x40\xff")

    with pytest.raises(ValueError, match="bin.dat"):
        load_context_file(str(path))


# ── 未知格式与错误处理 ─────────────────────────────────────────


def test_unknown_suffix_still_reads_as_text(tmp_path):
    path = write(tmp_path, "cfg.ini", "[section]\nkey=value")

    assert "key=value" in load_context_file(str(path))["text"]


def test_invalid_docx_reports_read_failure_with_clipboard_fallback(tmp_path):
    """损坏 DOCX 不得伪装成正文，并应给出替代路径。"""
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04fake")

    with pytest.raises(ValueError, match="剪贴板"):
        load_context_file(str(path))


def test_missing_file_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        load_context_file(str(tmp_path / "nope.txt"))


def test_directory_path_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="不是文件"):
        load_context_file(str(tmp_path))


def test_oversized_file_is_rejected_before_reading(tmp_path):
    path = write(tmp_path, "huge.txt", "x" * 5000)

    with pytest.raises(ValueError, match="过大"):
        load_context_file(str(path), max_bytes=1000)
