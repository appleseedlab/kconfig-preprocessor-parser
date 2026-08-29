import json
from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path
import re
from typing import Any, Literal

import tree_sitter_c as tsc
from tree_sitter import Language, Parser


PREPROC_TYPES = {"preproc_if", "preproc_ifdef", "preproc_elif"}
SKIP_TYPES = {"#ifdef", "#ifndef", "#else", "#if", "#elif", "#endif", "\n"}


@dataclass
class ConditionInfo:
    symbol: str | None = None
    polarity: Literal["positive", "negative"] = "positive"
    parseable: bool = False
    raw_condition: str | None = None
    failure_reason: str | None = None


@dataclass
class PreprocBlock:
    type: str
    start_line: int
    end_line: int
    body_lines: list[int] = field(default_factory=list)
    condition: ConditionInfo = field(default_factory=ConditionInfo)


@dataclass
class EffectiveConditionInfo:
    raw_expression: str | None = None
    parseable: bool = False
    failure_reason: str | None = None
    terms: list[ConditionInfo] = field(default_factory=list)


@dataclass
class ParserStats:
    parseable_lines: int = 0
    parseable_blocks: int = 0
    unparseable_lines: int = 0
    unparseable_blocks: int = 0


@dataclass
class ParseableLines:
    parseable_lines: dict[str, list[int]]
    stats: ParserStats


def _default_condition_info() -> ConditionInfo:
    return ConditionInfo()


def _condition_failure(raw_condition: str | None, reason: str) -> ConditionInfo:
    return ConditionInfo(
        raw_condition=raw_condition,
        failure_reason=reason,
    )


def _collect_body_lines(
    node, preproc_types: set[str], skip_types: set[str]
) -> set[int]:
    lines: set[int] = set()
    for child in node.children:
        if child.type in preproc_types:
            continue
        if child.type in {"preproc_elif", "preproc_else"}:
            lines.update(_collect_body_lines(child, preproc_types, skip_types))
            continue
        if child.type in skip_types:
            continue
        if child == node.child_by_field_name("name"):
            continue
        if child == node.child_by_field_name("condition"):
            continue
        start = child.start_point[0] + 1
        end = child.end_point[0] + 1
        for line in range(start, end + 1):
            lines.add(line)
    return lines


def _collect_branch_body_lines(
    node, preproc_types: set[str], skip_types: set[str]
) -> set[int]:
    lines: set[int] = set()
    for child in node.children:
        if child.type in preproc_types:
            continue
        if child.type in {"preproc_elif", "preproc_else"}:
            continue
        if child.type in skip_types:
            continue
        if child == node.child_by_field_name("name"):
            continue
        if child == node.child_by_field_name("condition"):
            continue
        start = child.start_point[0] + 1
        end = child.end_point[0] + 1
        for line in range(start, end + 1):
            lines.add(line)
    return lines


def _clone_condition_info(condition: ConditionInfo) -> ConditionInfo:
    return ConditionInfo(
        symbol=condition.symbol,
        polarity=condition.polarity,
        parseable=condition.parseable,
        raw_condition=condition.raw_condition,
        failure_reason=condition.failure_reason,
    )


def _negate_condition(condition: ConditionInfo) -> ConditionInfo:
    negated_polarity: Literal["positive", "negative"] = (
        "negative" if condition.polarity == "positive" else "positive"
    )
    return ConditionInfo(
        symbol=condition.symbol,
        polarity=negated_polarity,
        parseable=condition.parseable,
        raw_condition=condition.raw_condition,
        failure_reason=condition.failure_reason,
    )


def _condition_term_to_expression(term: ConditionInfo) -> str | None:
    if term.parseable and term.symbol:
        if term.polarity == "negative":
            return f"not {term.symbol}"
        return term.symbol

    raw_condition = term.raw_condition.strip() if term.raw_condition else None
    if not raw_condition:
        return None
    if term.polarity == "negative":
        return f"!({raw_condition})"
    return raw_condition


def _build_effective_condition_info(
    terms: list[ConditionInfo],
) -> EffectiveConditionInfo:
    expression_terms: list[str] = []
    all_parseable = True
    failure_reason: str | None = None

    for term in terms:
        expression_term = _condition_term_to_expression(term)
        if expression_term:
            expression_terms.append(expression_term)

        if not term.parseable or not term.symbol:
            all_parseable = False
            if not failure_reason:
                failure_reason = term.failure_reason or "contains_unparseable_term"

    raw_expression = " and ".join(expression_terms) if expression_terms else None
    if not terms:
        all_parseable = False
        failure_reason = "empty_effective_condition"

    return EffectiveConditionInfo(
        raw_expression=raw_expression,
        parseable=all_parseable,
        failure_reason=failure_reason,
        terms=[_clone_condition_info(term) for term in terms],
    )


def _find_next_branch_node(node):
    for child in node.children:
        if child.type in {"preproc_elif", "preproc_else"}:
            return child
    return None


def _collect_chain_nodes(root_node) -> tuple[list[Any], Any | None]:
    elif_nodes: list = []
    else_node = None

    next_branch = _find_next_branch_node(root_node)
    while next_branch:
        if next_branch.type == "preproc_elif":
            elif_nodes.append(next_branch)
            next_branch = _find_next_branch_node(next_branch)
            continue

        else_node = next_branch
        break

    return elif_nodes, else_node


def _iter_branch_children_for_traversal(node):
    for child in node.children:
        if child.type in {"preproc_elif", "preproc_else"}:
            continue
        if child.type in SKIP_TYPES:
            continue
        if child == node.child_by_field_name("name"):
            continue
        if child == node.child_by_field_name("condition"):
            continue
        yield child


def _build_else_local_condition(chain_conditions: list[ConditionInfo]) -> ConditionInfo:
    if len(chain_conditions) == 1:
        return _negate_condition(chain_conditions[0])

    negated_terms = [_negate_condition(condition) for condition in chain_conditions]
    expression_terms = [
        term
        for term in (
            _condition_term_to_expression(condition) for condition in negated_terms
        )
        if term
    ]
    raw_condition = " and ".join(expression_terms) if expression_terms else None
    return ConditionInfo(
        raw_condition=raw_condition,
        parseable=False,
        failure_reason="compound_else_branch_condition",
    )


def _branch_end_line(node, body_lines: list[int]) -> int:
    if body_lines:
        return body_lines[-1]
    return node.start_point[0] + 1


def _emit_branch_blocks_for_chain(
    node,
    preproc_types: set[str],
    skip_types: set[str],
    source_code: bytes,
    ancestor_terms: list[ConditionInfo],
    blocks: list[tuple[PreprocBlock, EffectiveConditionInfo]],
) -> None:
    elif_nodes, else_node = _collect_chain_nodes(node)
    branch_nodes = [node, *elif_nodes]

    chain_conditions = [
        _parse_preproc_condition(branch_node, source_code)
        for branch_node in branch_nodes
    ]

    for branch_index, branch_node in enumerate(branch_nodes):
        negated_prefix = [
            _negate_condition(condition)
            for condition in chain_conditions[:branch_index]
        ]
        local_terms = [*negated_prefix, chain_conditions[branch_index]]
        effective_terms = [*ancestor_terms, *local_terms]

        nested_blocks: list[tuple[PreprocBlock, EffectiveConditionInfo]] = []
        for child in _iter_branch_children_for_traversal(branch_node):
            _find_branch_blocks_in_node(
                child,
                preproc_types,
                skip_types,
                source_code,
                effective_terms,
                nested_blocks,
            )

        nested_body_lines = {
            line
            for nested_block, _ in nested_blocks
            for line in nested_block.body_lines
        }
        raw_branch_body_lines = set(
            _collect_branch_body_lines(branch_node, preproc_types, skip_types)
        )
        body_lines = sorted(raw_branch_body_lines - nested_body_lines)

        block = PreprocBlock(
            type=branch_node.type,
            start_line=branch_node.start_point[0] + 1,
            end_line=_branch_end_line(branch_node, body_lines),
            body_lines=body_lines,
            condition=_clone_condition_info(chain_conditions[branch_index]),
        )
        blocks.append((block, _build_effective_condition_info(effective_terms)))
        blocks.extend(nested_blocks)

    if not else_node:
        return

    else_local_terms = [_negate_condition(condition) for condition in chain_conditions]
    else_effective_terms = [*ancestor_terms, *else_local_terms]
    else_nested_blocks: list[tuple[PreprocBlock, EffectiveConditionInfo]] = []
    for child in _iter_branch_children_for_traversal(else_node):
        _find_branch_blocks_in_node(
            child,
            preproc_types,
            skip_types,
            source_code,
            else_effective_terms,
            else_nested_blocks,
        )

    else_nested_body_lines = {
        line
        for nested_block, _ in else_nested_blocks
        for line in nested_block.body_lines
    }
    raw_else_body_lines = set(
        _collect_branch_body_lines(else_node, preproc_types, skip_types)
    )
    else_body_lines = sorted(raw_else_body_lines - else_nested_body_lines)

    else_block = PreprocBlock(
        type=else_node.type,
        start_line=else_node.start_point[0] + 1,
        end_line=_branch_end_line(else_node, else_body_lines),
        body_lines=else_body_lines,
        condition=_build_else_local_condition(chain_conditions),
    )
    blocks.append((else_block, _build_effective_condition_info(else_effective_terms)))
    blocks.extend(else_nested_blocks)


def _find_branch_blocks_in_node(
    node,
    preproc_types: set[str],
    skip_types: set[str],
    source_code: bytes,
    ancestor_terms: list[ConditionInfo],
    blocks: list[tuple[PreprocBlock, EffectiveConditionInfo]],
) -> None:
    if node.type in {"preproc_if", "preproc_ifdef"}:
        _emit_branch_blocks_for_chain(
            node,
            preproc_types,
            skip_types,
            source_code,
            ancestor_terms,
            blocks,
        )
        return

    for child in node.children:
        _find_branch_blocks_in_node(
            child,
            preproc_types,
            skip_types,
            source_code,
            ancestor_terms,
            blocks,
        )


def _strip_outer_parens_once(text: str) -> str:
    stripped = text.strip()
    if not (stripped.startswith("(") and stripped.endswith(")")):
        return stripped

    depth = 0
    for idx, char in enumerate(stripped):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return stripped
            if depth == 0 and idx != len(stripped) - 1:
                return stripped

    if depth != 0:
        return stripped

    return stripped[1:-1].strip()


def _node_text(node, source_code: bytes) -> str:
    return source_code[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _normalize_defined_term_text(term_text: str) -> str | None:
    normalized_term = _strip_outer_parens_once(term_text)
    normalized_term = re.sub(r"\s+", " ", normalized_term).strip()
    if not normalized_term:
        return None

    defined_match = re.fullmatch(
        r"defined\s*(?:\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)|\s+(CONFIG_[A-Za-z0-9_]+))",
        normalized_term,
    )
    if defined_match:
        symbol = defined_match.group(1) or defined_match.group(2)
        return f"({symbol})"

    if re.fullmatch(r"CONFIG_[A-Za-z0-9_]+", normalized_term):
        return f"({normalized_term})"

    return None


def _binary_expression_parts(node, source_code: bytes):
    operator_node = None
    operands: list[Any] = []

    for child in node.children:
        if child.type in {"\n", "(", ")"}:
            continue
        if child.type in {"&&", "||"}:
            operator_node = child
            continue

        child_text = _node_text(child, source_code).strip()
        if child_text in {"&&", "||"}:
            operator_node = child
            continue

        operands.append(child)

    if len(operands) != 2 or not operator_node:
        return None

    return operands[0], operator_node, operands[1]


def _normalize_condition_expression_node(node, source_code: bytes) -> str | None:
    if node.type == "parenthesized_expression":
        inner_nodes = [
            child for child in node.children if child.type not in {"\n", "(", ")"}
        ]
        if len(inner_nodes) != 1:
            return None

        inner_expression = _normalize_condition_expression_node(
            inner_nodes[0], source_code
        )
        if not inner_expression:
            return None
        return inner_expression

    if node.type == "binary_expression":
        expression_parts = _binary_expression_parts(node, source_code)
        if not expression_parts:
            return None
        left_node, operator_node, right_node = expression_parts

        left_expression = _normalize_condition_expression_node(left_node, source_code)
        right_expression = _normalize_condition_expression_node(right_node, source_code)
        if not left_expression or not right_expression:
            return None

        operator_text = _node_text(operator_node, source_code).strip()
        if operator_text == "&&":
            operator_keyword = "and"
        elif operator_text == "||":
            operator_keyword = "or"
        else:
            return None

        return f"({left_expression} {operator_keyword} {right_expression})"

    return _normalize_defined_term_text(_node_text(node, source_code))


def _parse_condition_text(condition_text: str) -> ConditionInfo:
    condition_info = _default_condition_info()
    raw_condition = condition_text.strip()
    if not raw_condition:
        return _condition_failure(None, "empty_condition")

    condition_info.raw_condition = raw_condition
    normalized = raw_condition
    negated = False

    if normalized.startswith("!"):
        negated = True
        normalized = normalized[1:].strip()

    normalized = _strip_outer_parens_once(normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return _condition_failure(raw_condition, "empty_condition")

    defined_paren_match = re.match(
        r"^defined\s*\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)$",
        normalized,
    )
    if defined_paren_match:
        symbol = defined_paren_match.group(1)
        condition_info.symbol = symbol
        condition_info.polarity = "negative" if negated else "positive"
        canonical_raw = f"defined({symbol})"
        condition_info.raw_condition = (
            f"not {canonical_raw}" if negated else canonical_raw
        )
        condition_info.parseable = True
        return condition_info

    defined_space_match = re.match(
        r"^defined\s+(CONFIG_[A-Za-z0-9_]+)$",
        normalized,
    )
    if defined_space_match:
        symbol = defined_space_match.group(1)
        condition_info.symbol = symbol
        condition_info.polarity = "negative" if negated else "positive"
        canonical_raw = f"defined({symbol})"
        condition_info.raw_condition = (
            f"not {canonical_raw}" if negated else canonical_raw
        )
        condition_info.parseable = True
        return condition_info

    is_enabled_match = re.match(
        r"^IS_ENABLED\s*\(\s*(CONFIG_[A-Za-z0-9_]+)\s*\)$",
        normalized,
    )
    if is_enabled_match:
        symbol = is_enabled_match.group(1)
        condition_info.symbol = symbol
        condition_info.polarity = "negative" if negated else "positive"
        canonical_raw = f"IS_ENABLED({symbol})"
        condition_info.raw_condition = (
            f"not {canonical_raw}" if negated else canonical_raw
        )
        condition_info.parseable = True
        return condition_info

    bare_match = re.match(
        r"^\(*\s*(CONFIG_[A-Za-z0-9_]+)\s*\)*$",
        normalized,
    )
    if bare_match:
        symbol = bare_match.group(1)
        condition_info.symbol = symbol
        condition_info.polarity = "negative" if negated else "positive"
        condition_info.raw_condition = f"not {symbol}" if negated else symbol
        condition_info.parseable = True
        return condition_info

    return _condition_failure(raw_condition, "unsupported_condition_expression")


def _parse_preproc_condition(node, source_code: bytes) -> ConditionInfo:
    condition_info = _default_condition_info()

    if node.type == "preproc_ifdef":
        directive_text = source_code[node.start_byte : node.end_byte].decode(
            "utf-8", errors="ignore"
        )
        raw_condition = directive_text.strip()
        name_node = node.child_by_field_name("name")
        if not name_node:
            return _condition_failure(raw_condition, "missing_name_node")
        symbol = name_node.text.decode("utf-8")
        if not symbol.startswith("CONFIG_"):
            return _condition_failure(symbol, "non_config_symbol")
        condition_info.symbol = symbol
        condition_info.raw_condition = raw_condition
        condition_info.polarity = (
            "negative" if raw_condition.startswith("#ifndef") else "positive"
        )
        condition_info.parseable = True
        return condition_info

    if node.type in {"preproc_if", "preproc_elif"}:
        condition_node = node.child_by_field_name("condition")
        if not condition_node:
            raw_condition = source_code[node.start_byte : node.end_byte].decode(
                "utf-8", errors="ignore"
            )
            return _condition_failure(raw_condition.strip(), "missing_condition_node")

        condition_text = source_code[
            condition_node.start_byte : condition_node.end_byte
        ].decode("utf-8", errors="ignore")
        parsed_condition = _parse_condition_text(condition_text)
        if parsed_condition.parseable:
            return parsed_condition

        normalized_expression = _normalize_condition_expression_node(
            condition_node,
            source_code,
        )
        if not normalized_expression:
            return parsed_condition

        return ConditionInfo(
            symbol=normalized_expression,
            polarity="positive",
            parseable=True,
            raw_condition=normalized_expression,
        )

    return condition_info


def _find_blocks_in_node(
    node,
    preproc_types: set[str],
    skip_types: set[str],
    source_code: bytes,
    blocks: list[PreprocBlock],
) -> None:
    if node.type in preproc_types:
        condition_info = _parse_preproc_condition(node, source_code)
        body_lines = _collect_body_lines(node, preproc_types, skip_types)
        blocks.append(
            PreprocBlock(
                type=node.type,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                body_lines=sorted(body_lines),
                condition=condition_info,
            )
        )
    for child in node.children:
        _find_blocks_in_node(child, preproc_types, skip_types, source_code, blocks)


def get_function_name(node):
    """Extract function name from a function_definition node."""
    for child in node.children:
        if child.type == "function_declarator":
            for subchild in child.children:
                if subchild.type == "identifier":
                    return subchild.text.decode("utf-8")
        elif child.type == "pointer_declarator":
            return get_function_name(child)
        elif child.type == "identifier":
            return child.text.decode("utf-8")
    return None


def parse_function_spans(c_file: Path) -> dict[str, tuple[int, int]]:
    """Extract start and end line numbers for every function in a C file.

    Args:
        c_file: Path to the C source file.

    Returns:
        A dict mapping function names to (start_line, end_line) tuples.
        Line numbers are 1-indexed.
    """
    c_language = Language(tsc.language())
    parser = Parser(c_language)

    source_code = c_file.read_bytes()
    tree = parser.parse(source_code)

    result: dict[str, tuple[int, int]] = {}

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            func_name = get_function_name(node)
            if func_name:
                # tree-sitter uses 0-indexed lines, convert to 1-indexed
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                result[func_name] = (start_line, end_line)
        stack.extend(node.children)
    return result


def extract_preproc_blocks_from_covered_functions(
    kernel_src: Path,
    c_file_path: Path,
    covered_functions: set[str],
) -> list[PreprocBlock]:
    """Extract preprocessor directive blocks from covered functions."""
    c_language = Language(tsc.language())
    parser = Parser(c_language)

    source_code = (kernel_src / c_file_path).read_bytes()
    tree = parser.parse(source_code)

    blocks: list[PreprocBlock] = []

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            func_name = get_function_name(node)
            if func_name and func_name in covered_functions:
                _find_blocks_in_node(
                    node,
                    PREPROC_TYPES,
                    SKIP_TYPES,
                    source_code,
                    blocks,
                )
        else:
            stack.extend(node.children)

    return blocks


def extract_preproc_blocks_from_file(
    kernel_src: Path,
    c_file_path: Path,
) -> list[PreprocBlock]:
    """Extract all preprocessor directive blocks from a C file."""
    c_language = Language(tsc.language())
    parser = Parser(c_language)

    source_code = (kernel_src / c_file_path).read_bytes()
    tree = parser.parse(source_code)

    blocks: list[PreprocBlock] = []
    _find_blocks_in_node(
        tree.root_node,
        PREPROC_TYPES,
        SKIP_TYPES,
        source_code,
        blocks,
    )

    return blocks


def extract_preproc_branch_blocks_from_file(
    kernel_src: Path,
    c_file_path: Path,
) -> list[tuple[PreprocBlock, EffectiveConditionInfo]]:
    """Extract preprocessor directive branches with full effective conditions."""
    c_language = Language(tsc.language())
    parser = Parser(c_language)

    source_code = (kernel_src / c_file_path).read_bytes()
    tree = parser.parse(source_code)

    blocks: list[tuple[PreprocBlock, EffectiveConditionInfo]] = []
    _find_branch_blocks_in_node(
        tree.root_node,
        PREPROC_TYPES,
        SKIP_TYPES,
        source_code,
        [],
        blocks,
    )

    return blocks


def _line_raw_expression(
    line_number: int,
    branch_blocks: list[tuple[PreprocBlock, EffectiveConditionInfo]],
    rel_path: str,
) -> tuple[str | None, bool, str | None]:
    matches = [
        (block, effective_condition)
        for block, effective_condition in branch_blocks
        if line_number in block.body_lines
    ]

    if not matches:
        return None, False, None

    if len(matches) > 1:
        matches_by_start = sorted(matches, key=lambda match: match[0].start_line, reverse=True)
        top_start_line = matches_by_start[0][0].start_line
        top_matches = [
            (block, effective_condition)
            for block, effective_condition in matches_by_start
            if block.start_line == top_start_line
        ]
        if len(top_matches) == 1:
            effective_condition = top_matches[0][1]
            is_unresolved = not effective_condition.parseable
            failure_reason = (
                effective_condition.failure_reason if is_unresolved else None
            )
            return effective_condition.raw_expression, is_unresolved, failure_reason

        canonical_matches = {
            (
                effective_condition.raw_expression,
                effective_condition.parseable,
                effective_condition.failure_reason,
            )
            for _, effective_condition in top_matches
        }
        if len(canonical_matches) == 1:
            effective_condition = top_matches[0][1]
            is_unresolved = not effective_condition.parseable
            failure_reason = (
                effective_condition.failure_reason if is_unresolved else None
            )
            return effective_condition.raw_expression, is_unresolved, failure_reason

        raise ValueError(
            "Found ambiguous effective conditions for "
            f"{rel_path}:{line_number}; candidates={len(top_matches)}"
        )

    effective_condition = matches[0][1]
    is_unresolved = not effective_condition.parseable
    failure_reason = effective_condition.failure_reason if is_unresolved else None
    return effective_condition.raw_expression, is_unresolved, failure_reason


def _kernel_relative_from_absolute_filename(
    filename: str | Path,
    kernel_src: Path,
    strip_prefix: str | Path | None = None,
) -> Path | None:
    """Map a path recorded at build time onto a path relative to `kernel_src`.

    Coverage reports record absolute paths from the machine the kernel was built
    on, which rarely match the tree being analyzed now. Three strategies are
    tried in order:

    1. `strip_prefix`, when given, is removed from the front of the path.
    2. A path already relative, or already under `kernel_src`, is used directly.
    3. Otherwise the longest trailing portion of the path that exists under
       `kernel_src` is used.

    Returns None when no strategy yields a path that exists, so a single
    unrecognized entry skips instead of aborting the whole report.
    """
    filename_path = Path(filename)

    if strip_prefix is not None:
        try:
            return filename_path.relative_to(Path(strip_prefix))
        except ValueError:
            pass

    if not filename_path.is_absolute():
        return filename_path

    try:
        return filename_path.relative_to(kernel_src)
    except ValueError:
        pass

    # Longest trailing portion that resolves under kernel_src wins, so
    # "fs/ext4/inode.c" is preferred over the ambiguous "ext4/inode.c".
    parts = filename_path.parts
    for idx in range(1, len(parts)):
        candidate = Path(*parts[idx:])
        if (kernel_src / candidate).exists():
            return candidate

    return None


def _build_effective_conditions_results(
    kernel_src: Path,
    requested_lines: dict[str, list[int]],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, int], set[str]]:
    resolved_results: dict[str, list[dict]] = {}
    unresolved_results: dict[str, list[dict]] = {}
    raw_expressions: set[str] = set()
    total_requested_lines = 0
    resolved_lines = 0
    unresolved_lines = 0

    for rel_path, lines in sorted(requested_lines.items()):
        if not rel_path.endswith(".c"):
            continue

        full_path = kernel_src / rel_path
        if not full_path.exists():
            continue

        branch_blocks = extract_preproc_branch_blocks_from_file(
            kernel_src,
            Path(rel_path),
        )

        resolved_entries: list[dict] = []
        for line_number in lines:
            total_requested_lines += 1
            raw_expression, is_unresolved, failure_reason = _line_raw_expression(
                line_number,
                branch_blocks,
                rel_path,
            )

            if is_unresolved:
                unresolved_lines += 1
                unresolved_entry = {
                    "line": line_number,
                    "raw_expression": raw_expression,
                    "failure_reason": failure_reason,
                }
                if rel_path not in unresolved_results:
                    unresolved_results[rel_path] = []
                unresolved_results[rel_path].append(unresolved_entry)
                continue

            resolved_lines += 1
            resolved_entries.append(
                {
                    "line": line_number,
                    "raw_expression": raw_expression,
                }
            )
            if raw_expression:
                raw_expressions.add(raw_expression)

        if resolved_entries:
            resolved_results[rel_path] = resolved_entries

    summary = {
        "total_requested_lines": total_requested_lines,
        "resolved_lines": resolved_lines,
        "unresolved_lines": unresolved_lines,
    }
    return resolved_results, unresolved_results, summary, raw_expressions


def write_effective_conditions_for_requested_lines(
    kernel_src: Path,
    requested_lines: dict[str, list[int]],
    output_dir: Path,
) -> dict[str, Path]:
    (
        resolved_results,
        unresolved_results,
        summary,
        raw_expressions,
    ) = _build_effective_conditions_results(kernel_src, requested_lines)

    raw_output_path = (output_dir / "raw_effective_conditions.txt").absolute()
    resolved_output_path = (output_dir / "resolved_lines.json").absolute()
    unresolved_output_path = (output_dir / "unresolved_lines.json").absolute()
    summary_output_path = (output_dir / "summary.json").absolute()

    raw_output_path.write_text(
        "\n".join(raw_expressions) + ("\n" if raw_expressions else "")
    )

    resolved_payload = {
        "kernel_src": str(kernel_src),
        "resolved_results": resolved_results,
    }
    resolved_output_path.write_text(json.dumps(resolved_payload, indent=2))

    unresolved_payload = {
        "kernel_src": str(kernel_src),
        "unresolved_results": unresolved_results,
    }
    unresolved_output_path.write_text(json.dumps(unresolved_payload, indent=2))

    summary_payload = {
        "kernel_src": str(kernel_src),
        "summary": summary,
    }
    summary_output_path.write_text(json.dumps(summary_payload, indent=2))

    return {
        "raw_effective_conditions": raw_output_path,
        "resolved_lines": resolved_output_path,
        "unresolved_lines": unresolved_output_path,
        "summary": summary_output_path,
    }


def parse_enabled_configs(config_path: Path) -> set[str]:
    """Parse enabled CONFIG_* symbols from a kernel .config file."""
    enabled: set[str] = set()
    for line in config_path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("CONFIG_"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value in {"y", "m"}:
            enabled.add(key)
    return enabled


def extract_all_tracked_lines(coverage_file: Path) -> defaultdict[str, set[int]]:
    """Extract all tracked line numbers from a syzkaller coverage JSON report.

    Args:
        coverage_file: Path to the syzkaller coverage JSON file.

    Returns:
        A defaultdict mapping filenames to sets of all tracked line numbers.
        Includes lines from "Covered", "Uncovered", and "Both" categories.
    """
    result: defaultdict[str, set[int]] = defaultdict(set)

    with open(coverage_file) as f:
        data = json.load(f)

    for entry in data:
        filename = entry.get("Filename")
        if filename:
            covered = entry.get("Covered", [])
            uncovered = entry.get("Uncovered", [])
            both = entry.get("Both", [])
            result[filename].update(covered + uncovered + both)

    return result


def find_function_for_line(
    line: int, function_spans: dict[str, tuple[int, int]]
) -> str | None:
    """Find which function contains a given line number.

    Args:
        line: The line number to look up.
        function_spans: Dict mapping function names to (start_line, end_line) tuples.

    Returns:
        The function name if found, None otherwise.
    """
    for func_name, (start, end) in function_spans.items():
        if start <= line <= end:
            return func_name
    return None


def get_all_parseable_preproc_lines_in_covered_functions(
    coverage_file: Path,
    kernel_src: Path,
    strip_prefix: str | Path | None = None,
) -> ParseableLines:
    """Collect all parseable preprocessor body lines inside covered functions.

    Returns lines for both enabled and disabled parseable directives, plus stats.

    Args:
        coverage_file: Path to the syzkaller coverage JSON report.
        kernel_src: Root of the kernel tree to analyze.
        strip_prefix: Build-time path prefix to strip from the paths recorded in
            the report. Optional; when omitted, paths are matched against
            `kernel_src` by longest trailing portion. Files that cannot be
            mapped onto `kernel_src` are skipped.
    """

    all_tracked_lines = extract_all_tracked_lines(coverage_file)
    parseable_lines = defaultdict(list)
    stats = ParserStats()
    result = ParseableLines(parseable_lines=parseable_lines, stats=stats)

    for filename, tracked in all_tracked_lines.items():
        if not filename.endswith(".c"):
            continue
        if not tracked:
            continue

        rel_c_file_path = _kernel_relative_from_absolute_filename(
            filename, kernel_src, strip_prefix
        )
        if rel_c_file_path is None:
            continue
        c_file_path = kernel_src / rel_c_file_path
        if not c_file_path.exists():
            continue

        function_spans = parse_function_spans(c_file_path)
        if not function_spans:
            continue

        covered_functions: set[str] = set()
        for line in tracked:
            func_name = find_function_for_line(line, function_spans)
            if func_name:
                covered_functions.add(func_name)

        if not covered_functions:
            continue

        blocks = extract_preproc_blocks_from_covered_functions(
            kernel_src, rel_c_file_path, covered_functions
        )
        if not blocks:
            continue

        lines: set[int] = set()
        for block in blocks:
            body_lines = block.body_lines
            condition = block.condition
            if not condition.parseable:
                stats.unparseable_blocks += 1
                stats.unparseable_lines += len(body_lines)
                continue

            symbol = condition.symbol
            if not symbol:
                stats.unparseable_blocks += 1
                stats.unparseable_lines += len(body_lines)
                continue

            stats.parseable_blocks += 1
            stats.parseable_lines += len(body_lines)
            lines.update(body_lines)

        if lines:
            parseable_lines[str(rel_c_file_path)] = sorted(lines)

    return result


def get_all_lines_in_preproc(
    coverage_file: Path,
    kernel_src: Path,
    output_file: Path,
    strip_prefix: str | Path | None = None,
) -> ParseableLines:
    parseable_result = get_all_parseable_preproc_lines_in_covered_functions(
        coverage_file, kernel_src, strip_prefix
    )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        for file_path, lines in sorted(parseable_result.parseable_lines.items()):
            line_nums = ",".join(str(line) for line in lines)
            f.write(f"{file_path}:[{line_nums}]\n")

    return parseable_result
