import json
from pathlib import Path

import pytest


from kconfig_preprocessor_parser.parser import (
    EffectiveConditionInfo,
    PreprocBlock,
    _line_raw_expression,
    extract_preproc_blocks_from_file,
    extract_preproc_branch_blocks_from_file,
    get_all_parseable_preproc_lines_in_covered_functions,
)


def test_extract_preproc_blocks_from_file_returns_all_file_blocks(tmp_path: Path):
    source_lines = [
        "#ifdef CONFIG_TOP",
        "int top_level = 1;",
        "#endif",
        "int fn_a(void) {",
        "#ifdef CONFIG_A",
        "1;",
        "#endif",
        "return 0;",
        "}",
        "int fn_b(void) {",
        "#if ! defined(CONFIG_B)",
        "2;",
        "#endif",
        "return 0;",
        "}",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    blocks = extract_preproc_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    by_start = {block.start_line: block for block in blocks}

    assert set(by_start.keys()) == {1, 5, 11}

    top = by_start[1]
    top_condition = top.condition
    assert top.body_lines == [2]
    assert top_condition.symbol == "CONFIG_TOP"
    assert top_condition.polarity == "positive"
    assert top_condition.parseable is True
    assert top_condition.raw_condition is not None
    assert "CONFIG_TOP" in top_condition.raw_condition
    assert top_condition.failure_reason is None

    fn_a = by_start[5]
    fn_a_condition = fn_a.condition
    assert fn_a.body_lines == [6]
    assert fn_a_condition.symbol == "CONFIG_A"
    assert fn_a_condition.polarity == "positive"
    assert fn_a_condition.parseable is True
    assert fn_a_condition.raw_condition is not None
    assert "CONFIG_A" in fn_a_condition.raw_condition
    assert fn_a_condition.failure_reason is None

    fn_b = by_start[11]
    fn_b_condition = fn_b.condition
    assert fn_b.body_lines == [12]
    assert fn_b_condition.symbol == "CONFIG_B"
    assert fn_b_condition.polarity == "negative"
    assert fn_b_condition.parseable is True
    assert fn_b_condition.raw_condition == "not defined(CONFIG_B)"
    assert fn_b_condition.failure_reason is None


def test_extract_preproc_blocks_from_file_keeps_unparseable_context(tmp_path: Path):
    source_lines = [
        "int fn_a(void) {",
        "#if defined(CONFIG_H) && (MACRO > 10)",
        "1;",
        "#endif",
        "return 0;",
        "}",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    blocks = extract_preproc_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    assert len(blocks) == 1
    condition = blocks[0].condition
    assert condition.parseable is False
    assert condition.symbol is None
    assert condition.raw_condition == "defined(CONFIG_H) && (MACRO > 10)"
    assert condition.failure_reason == "unsupported_condition_expression"


def test_extract_preproc_blocks_from_file_normalizes_defined_or_chain(tmp_path: Path):
    source_lines = [
        "int fn_a(void) {",
        "#if defined(CONFIG_X86) || defined CONFIG_M68K",
        "1;",
        "#endif",
        "return 0;",
        "}",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    blocks = extract_preproc_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    assert len(blocks) == 1
    condition = blocks[0].condition
    assert condition.parseable is True
    assert condition.symbol == "((CONFIG_X86) or (CONFIG_M68K))"
    assert condition.raw_condition == "((CONFIG_X86) or (CONFIG_M68K))"
    assert condition.failure_reason is None


def test_extract_preproc_blocks_from_file_normalizes_defined_and_chain(tmp_path: Path):
    source_lines = [
        "int fn_a(void) {",
        "#if defined CONFIG_A && defined(CONFIG_B)",
        "1;",
        "#endif",
        "return 0;",
        "}",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    blocks = extract_preproc_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    assert len(blocks) == 1
    condition = blocks[0].condition
    assert condition.parseable is True
    assert condition.symbol == "((CONFIG_A) and (CONFIG_B))"
    assert condition.raw_condition == "((CONFIG_A) and (CONFIG_B))"
    assert condition.failure_reason is None


def test_extract_preproc_blocks_from_file_normalizes_nested_defined_expression(
    tmp_path: Path,
):
    source_lines = [
        "int fn_a(void) {",
        "#if defined(CONFIG_A) && (defined CONFIG_B || defined(CONFIG_C))",
        "1;",
        "#endif",
        "return 0;",
        "}",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    blocks = extract_preproc_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    assert len(blocks) == 1
    condition = blocks[0].condition
    assert condition.parseable is True
    assert condition.symbol == "((CONFIG_A) and ((CONFIG_B) or (CONFIG_C)))"
    assert condition.raw_condition == "((CONFIG_A) and ((CONFIG_B) or (CONFIG_C)))"
    assert condition.failure_reason is None


def test_extract_preproc_blocks_from_file_normalizes_multi_term_defined_chain(
    tmp_path: Path,
):
    source_lines = [
        "int fn_a(void) {",
        "#if defined CONFIG_A && defined(CONFIG_B) && defined CONFIG_C",
        "1;",
        "#endif",
        "return 0;",
        "}",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    blocks = extract_preproc_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    assert len(blocks) == 1
    condition = blocks[0].condition
    assert condition.parseable is True
    assert condition.symbol == "(((CONFIG_A) and (CONFIG_B)) and (CONFIG_C))"
    assert condition.raw_condition == "(((CONFIG_A) and (CONFIG_B)) and (CONFIG_C))"
    assert condition.failure_reason is None


def test_extract_preproc_branch_blocks_from_file_splits_else_branch(tmp_path: Path):
    source_lines = [
        "#ifdef CONFIG_A",
        "4;",
        "#else",
        "5;",
        "#endif",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    branch_blocks = extract_preproc_branch_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    by_start = {
        block.start_line: (block, effective) for block, effective in branch_blocks
    }
    assert set(by_start.keys()) == {1, 3}

    if_block, if_effective = by_start[1]
    assert if_block.type == "preproc_ifdef"
    assert if_block.body_lines == [2]
    assert if_block.condition.symbol == "CONFIG_A"
    assert if_block.condition.polarity == "positive"
    assert if_effective.raw_expression == "CONFIG_A"
    assert if_effective.parseable is True
    assert if_effective.failure_reason is None

    else_block, else_effective = by_start[3]
    assert else_block.type == "preproc_else"
    assert else_block.body_lines == [4]
    assert else_block.condition.symbol == "CONFIG_A"
    assert else_block.condition.polarity == "negative"
    assert else_block.condition.parseable is True
    assert else_effective.raw_expression == "not CONFIG_A"
    assert else_effective.parseable is True
    assert else_effective.failure_reason is None


def test_extract_preproc_branch_blocks_from_file_includes_ancestor_context(
    tmp_path: Path,
):
    source_lines = [
        "#ifdef CONFIG_A",
        "#if defined(CONFIG_B)",
        "1;",
        "#elif defined(CONFIG_C)",
        "2;",
        "#else",
        "3;",
        "#endif",
        "#endif",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    branch_blocks = extract_preproc_branch_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    by_start = {
        block.start_line: (block, effective) for block, effective in branch_blocks
    }
    assert set(by_start.keys()) == {1, 2, 4, 6}

    root_block, root_effective = by_start[1]
    assert root_block.body_lines == []
    assert root_effective.raw_expression == "CONFIG_A"
    assert root_effective.parseable is True

    if_block, if_effective = by_start[2]
    assert if_block.body_lines == [3]
    assert if_effective.raw_expression == "CONFIG_A and CONFIG_B"
    assert if_effective.parseable is True

    elif_block, elif_effective = by_start[4]
    assert elif_block.body_lines == [5]
    assert elif_effective.raw_expression == "CONFIG_A and not CONFIG_B and CONFIG_C"
    assert elif_effective.parseable is True

    else_block, else_effective = by_start[6]
    assert else_block.body_lines == [7]
    assert else_block.condition.parseable is False
    assert else_block.condition.failure_reason == "compound_else_branch_condition"
    assert else_block.condition.raw_condition == "not CONFIG_B and not CONFIG_C"
    assert else_effective.raw_expression == "CONFIG_A and not CONFIG_B and not CONFIG_C"
    assert else_effective.parseable is True


def test_extract_preproc_branch_blocks_from_file_keeps_unparseable_terms(
    tmp_path: Path,
):
    source_lines = [
        "#if defined(CONFIG_H) && (MACRO > 10)",
        "1;",
        "#elif defined(CONFIG_I)",
        "2;",
        "#else",
        "3;",
        "#endif",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    branch_blocks = extract_preproc_branch_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    by_start = {
        block.start_line: (block, effective) for block, effective in branch_blocks
    }
    assert set(by_start.keys()) == {1, 3, 5}

    if_block, if_effective = by_start[1]
    assert if_block.condition.parseable is False
    assert if_effective.raw_expression == "defined(CONFIG_H) && (MACRO > 10)"
    assert if_effective.parseable is False
    assert if_effective.failure_reason == "unsupported_condition_expression"

    elif_block, elif_effective = by_start[3]
    assert elif_block.condition.parseable is True
    assert elif_effective.raw_expression == (
        "!(defined(CONFIG_H) && (MACRO > 10)) and CONFIG_I"
    )
    assert elif_effective.parseable is False
    assert elif_effective.failure_reason == "unsupported_condition_expression"

    else_block, else_effective = by_start[5]
    assert else_block.condition.parseable is False
    assert else_block.condition.failure_reason == "compound_else_branch_condition"
    assert (
        else_effective.raw_expression
        == "!(defined(CONFIG_H) && (MACRO > 10)) and not CONFIG_I"
    )
    assert else_effective.parseable is False
    assert else_effective.failure_reason == "unsupported_condition_expression"


def test_extract_preproc_branch_blocks_from_file_excludes_nested_lines_from_parent(
    tmp_path: Path,
):
    source_lines = [
        "#ifdef CONFIG_A",
        "line_34;",
        "#ifdef CONFIG_B",
        "line_35;",
        "line_36;",
        "#endif",
        "#endif",
    ]
    c_file = tmp_path / "sample.c"
    c_file.write_text("\n".join(source_lines))

    branch_blocks = extract_preproc_branch_blocks_from_file(
        kernel_src=tmp_path,
        c_file_path=Path("sample.c"),
    )

    by_start = {
        block.start_line: (block, effective) for block, effective in branch_blocks
    }
    assert set(by_start.keys()) == {1, 3}

    outer_block, outer_effective = by_start[1]
    assert outer_block.body_lines == [2]
    assert outer_effective.raw_expression == "CONFIG_A"

    inner_block, inner_effective = by_start[3]
    assert inner_block.body_lines == [4, 5]
    assert inner_effective.raw_expression == "CONFIG_A and CONFIG_B"


def test_line_raw_expression_prefers_most_specific_match():
    line_number = 12
    outer_block = PreprocBlock(
        type="preproc_ifdef",
        start_line=10,
        end_line=20,
        body_lines=[11, 12, 13],
    )
    outer_effective = EffectiveConditionInfo(
        raw_expression="CONFIG_X86_64",
        parseable=True,
    )

    inner_block = PreprocBlock(
        type="preproc_else",
        start_line=11,
        end_line=13,
        body_lines=[12],
    )
    inner_effective = EffectiveConditionInfo(
        raw_expression="not CONFIG_X86_64",
        parseable=True,
    )

    raw_expression, is_unresolved, failure_reason = _line_raw_expression(
        line_number,
        [(outer_block, outer_effective), (inner_block, inner_effective)],
        "sample.c",
    )

    assert raw_expression == "not CONFIG_X86_64"
    assert is_unresolved is False
    assert failure_reason is None


def _kernel_tree(tmp_path: Path) -> Path:
    kernel_src = tmp_path / "linux"
    (kernel_src / "fs" / "ext4").mkdir(parents=True)
    (kernel_src / "fs" / "ext4" / "inode.c").write_text(
        "int fn(void) {\n#ifdef CONFIG_A\na();\n#endif\nreturn 0;\n}\n"
    )
    return kernel_src


def _coverage_file(tmp_path: Path, filename: str) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps(
            [{"Filename": filename, "Covered": [1, 2, 3], "Uncovered": [], "Both": []}]
        )
    )
    return path


@pytest.mark.parametrize(
    "filename",
    [
        "/home/x/research/linux_clone_3/fs/ext4/inode.c",
        "/build/ci/kernel-6.1/fs/ext4/inode.c",
        "fs/ext4/inode.c",
    ],
    ids=["legacy_linux_clone_layout", "unrelated_build_root", "already_relative"],
)
def test_coverage_paths_map_onto_kernel_src_without_a_prefix(
    tmp_path: Path, filename: str
):
    kernel_src = _kernel_tree(tmp_path)

    result = get_all_parseable_preproc_lines_in_covered_functions(
        coverage_file=_coverage_file(tmp_path, filename),
        kernel_src=kernel_src,
    )

    assert dict(result.parseable_lines) == {"fs/ext4/inode.c": [3]}


def test_strip_prefix_maps_coverage_paths_onto_kernel_src(tmp_path: Path):
    kernel_src = _kernel_tree(tmp_path)

    result = get_all_parseable_preproc_lines_in_covered_functions(
        coverage_file=_coverage_file(tmp_path, "/opt/src/fs/ext4/inode.c"),
        kernel_src=kernel_src,
        strip_prefix="/opt/src",
    )

    assert dict(result.parseable_lines) == {"fs/ext4/inode.c": [3]}


def test_unmappable_coverage_path_is_skipped_rather_than_raising(tmp_path: Path):
    kernel_src = _kernel_tree(tmp_path)

    result = get_all_parseable_preproc_lines_in_covered_functions(
        coverage_file=_coverage_file(tmp_path, "/nowhere/absent.c"),
        kernel_src=kernel_src,
    )

    assert dict(result.parseable_lines) == {}


def test_longest_matching_path_suffix_wins(tmp_path: Path):
    """An ambiguous tail must not shadow the correct, longer match."""
    kernel_src = _kernel_tree(tmp_path)
    (kernel_src / "ext4").mkdir()
    (kernel_src / "ext4" / "inode.c").write_text("int decoy(void) { return 0; }\n")

    result = get_all_parseable_preproc_lines_in_covered_functions(
        coverage_file=_coverage_file(tmp_path, "/build/x/fs/ext4/inode.c"),
        kernel_src=kernel_src,
    )

    assert dict(result.parseable_lines) == {"fs/ext4/inode.c": [3]}
