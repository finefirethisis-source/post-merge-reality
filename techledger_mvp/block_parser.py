"""Language-aware function/method extraction for the TechLedger MVP churn scan."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from tree_sitter_language_pack import get_parser


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "c_sharp",
}

FUNCTION_NODE_TYPES = {
    "function_definition",
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
    "local_function_statement",
    "operator_declaration",
    "conversion_operator_declaration",
}

CLASS_NODE_TYPES = {
    "class_definition",
    "class_declaration",
    "struct_specifier",
    "struct_declaration",
    "interface_declaration",
    "namespace_definition",
    "namespace_declaration",
}

ARROW_NODE_TYPES = {"arrow_function"}

BRANCH_PATTERN = re.compile(
    r"\b(if|for|while|case|catch|except|elif|switch|match)\b|&&|\|\||\?"
)
COMMENT_LINE_PATTERN = re.compile(r"^\s*(#|//|/\*|\*|<!--)")


@dataclass(frozen=True)
class Block:
    """One named function/method-like source block."""

    name: str
    start_line: int
    end_line: int
    loc: int
    complexity_proxy: int
    comment_fraction: float


def language_for_path(path: str) -> str | None:
    lower = path.lower()
    for suffix, language in LANGUAGE_BY_SUFFIX.items():
        if lower.endswith(suffix):
            return language
    return None


@lru_cache(maxsize=None)
def parser_for(language: str):
    return get_parser(language)


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _named_child_text(source: bytes, node, field: str = "name") -> str | None:
    child = node.child_by_field_name(field)
    if child is None:
        return None
    value = _node_text(source, child).strip()
    return value or None


def _arrow_name(source: bytes, node) -> str | None:
    parent = node.parent
    if parent is None:
        return None
    if parent.type in {"variable_declarator", "pair", "public_field_definition"}:
        return _named_child_text(source, parent)
    return None


def _scope_name(source: bytes, node) -> str | None:
    name = _named_child_text(source, node)
    if name:
        return name
    if node.type.startswith("namespace"):
        return _named_child_text(source, node, "name")
    return None


def _metrics(text: str) -> tuple[int, int, float]:
    lines = text.splitlines() or [""]
    loc = max(1, len(lines))
    complexity = len(BRANCH_PATTERN.findall(text))
    comment_lines = sum(bool(COMMENT_LINE_PATTERN.match(line)) for line in lines)
    return loc, complexity, comment_lines / loc


def extract_blocks(path: str, text: str) -> list[Block]:
    """Extract named function/method blocks from one source file snapshot.

    Qualified names include containing class/namespace/function scopes when available.
    Anonymous functions are ignored unless an arrow function is assigned to a named symbol.
    """

    language = language_for_path(path)
    if language is None or not text:
        return []

    source = text.encode("utf-8", errors="replace")
    tree = parser_for(language).parse(source)
    blocks: list[Block] = []

    def walk(node, scopes: tuple[str, ...]) -> None:
        next_scopes = scopes
        if node.type in CLASS_NODE_TYPES:
            scope = _scope_name(source, node)
            if scope:
                next_scopes = scopes + (scope,)

        is_function = node.type in FUNCTION_NODE_TYPES
        is_arrow = node.type in ARROW_NODE_TYPES
        if is_function or is_arrow:
            name = _named_child_text(source, node) if is_function else _arrow_name(source, node)
            if name:
                qualified = "::".join(scopes + (name,))
                body = _node_text(source, node)
                loc, complexity, comment_fraction = _metrics(body)
                blocks.append(
                    Block(
                        name=qualified,
                        start_line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        loc=loc,
                        complexity_proxy=complexity,
                        comment_fraction=comment_fraction,
                    )
                )
                next_scopes = scopes + (name,)

        for child in node.named_children:
            walk(child, next_scopes)

    walk(tree.root_node, ())

    # Keep the widest interval for duplicate symbol keys such as overloads. This treats
    # an overload family as one logical block for this illustrative MVP.
    by_name: dict[str, Block] = {}
    for block in blocks:
        previous = by_name.get(block.name)
        if previous is None or block.loc > previous.loc:
            by_name[block.name] = block
    return list(by_name.values())
