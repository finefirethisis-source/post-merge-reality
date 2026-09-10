"""Pure-Python named function/method extraction for the TechLedger MVP churn scan.

Python uses the standard-library AST. TypeScript/JavaScript, C# and C/C++ use a
conservative signature detector plus lexical brace matching. The goal here is a
stable, inspectable MVP block definition, not a complete language front end.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".h": "cpp",
    ".cs": "c_sharp",
}

BRANCH_PATTERN = re.compile(
    r"\b(if|for|while|case|catch|except|elif|switch|match)\b|&&|\|\||\?"
)
COMMENT_LINE_PATTERN = re.compile(r"^\s*(#|//|/\*|\*|<!--)")
CONTROL_NAMES = {
    "if", "for", "while", "switch", "catch", "foreach", "using", "lock",
    "return", "throw", "new", "sizeof", "typeof", "nameof", "checked",
    "unchecked", "fixed", "do", "else", "try",
}

# Explicit JS/TS declarations and assigned arrow functions.
JS_FUNCTION_RE = re.compile(
    r"(?:^|\s)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\("
)
ARROW_RE = re.compile(
    r"(?:^|\s)(?:export\s+)?(?:const|let|var)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*=.*?=>\s*\{"
)

# General method/function signature. It intentionally requires a nearby opening
# brace and rejects control-flow keywords; this catches ordinary C/C++/C# and
# class-style JS/TS methods without pretending to parse every language feature.
GENERAL_FUNCTION_RE = re.compile(
    r"(?P<name>[A-Za-z_$~][\w$~]*)\s*\([^;{}]*\)"
    r"(?:\s*(?::|->)\s*[^={;]+)?\s*\{\s*$"
)


@dataclass(frozen=True)
class Block:
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


def _metrics(lines: list[str], start_line: int, end_line: int) -> tuple[int, int, float]:
    start = max(1, start_line)
    end = min(len(lines), max(start, end_line))
    block_lines = lines[start - 1 : end]
    text = "\n".join(block_lines)
    loc = max(1, len(block_lines))
    complexity = len(BRANCH_PATTERN.findall(text))
    comment_lines = sum(bool(COMMENT_LINE_PATTERN.match(line)) for line in block_lines)
    return loc, complexity, comment_lines / loc


def _extract_python(text: str) -> list[Block]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []

    lines = text.splitlines()
    blocks: list[Block] = []

    def walk(node: ast.AST, scopes: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, scopes + (child.name,))
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = "::".join(scopes + (child.name,))
                start = child.lineno
                end = getattr(child, "end_lineno", child.lineno)
                loc, complexity, comment_fraction = _metrics(lines, start, end)
                blocks.append(Block(name, start, end, loc, complexity, comment_fraction))
                walk(child, scopes + (child.name,))
                continue
            walk(child, scopes)

    walk(tree, ())
    return blocks


def _brace_pairs(text: str) -> dict[int, int]:
    """Return character-index pairs for braces outside strings and comments."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    i = 0
    n = len(text)
    quote: str | None = None
    triple: str | None = None
    line_comment = False
    block_comment = False
    escaped = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if triple:
            if text.startswith(triple, i):
                i += len(triple)
                triple = None
            else:
                i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if text.startswith('"""', i) or text.startswith("'''", i):
            triple = text[i : i + 3]
            i += 3
            continue
        if ch in {'"', "'", "`"}:
            quote = ch
            i += 1
            continue
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            pairs[start] = i
        i += 1
    return pairs


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for match in re.finditer("\n", text):
        starts.append(match.end())
    return starts


def _line_number(starts: list[int], char_index: int) -> int:
    import bisect
    return bisect.bisect_right(starts, char_index)


def _candidate_signatures(text: str) -> list[tuple[str, int, int]]:
    """Return (name, signature_start_index, opening_brace_index)."""
    candidates: list[tuple[str, int, int]] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    rolling: list[tuple[int, str]] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        line_start = offset
        offset += len(raw_line)

        if not stripped:
            rolling = []
            continue
        rolling.append((line_start, stripped))
        rolling = rolling[-6:]

        if "{" not in stripped:
            continue

        joined = " ".join(part for _, part in rolling)
        signature_start = rolling[0][0]
        brace_in_line = raw_line.find("{")
        brace_index = line_start + brace_in_line

        match = JS_FUNCTION_RE.search(joined)
        if match:
            candidates.append((match.group("name"), signature_start, brace_index))
            rolling = []
            continue

        match = ARROW_RE.search(joined)
        if match:
            candidates.append((match.group("name"), signature_start, brace_index))
            rolling = []
            continue

        # Keep only the suffix ending at this opening brace so earlier unrelated
        # statements in the rolling window cannot create a false signature.
        suffix = joined[: joined.rfind("{") + 1]
        match = GENERAL_FUNCTION_RE.search(suffix)
        if match:
            name = match.group("name")
            if name.lower() not in CONTROL_NAMES:
                candidates.append((name, signature_start, brace_index))
                rolling = []
                continue

        # An opening brace that was not a function boundary breaks the signature
        # accumulation (class/namespace/object literal/control block, etc.).
        rolling = []

    return candidates


def _extract_braced(text: str) -> list[Block]:
    lines = text.splitlines()
    if not lines:
        return []
    pairs = _brace_pairs(text)
    starts = _line_starts(text)
    blocks: list[Block] = []

    for name, signature_start, brace_index in _candidate_signatures(text):
        close_index = pairs.get(brace_index)
        if close_index is None:
            continue
        start_line = _line_number(starts, signature_start)
        end_line = _line_number(starts, close_index)
        loc, complexity, comment_fraction = _metrics(lines, start_line, end_line)
        blocks.append(Block(name, start_line, end_line, loc, complexity, comment_fraction))

    # Duplicate names (overloads) are represented by the widest interval. File path
    # remains part of the scanner's identity, so same names in different files are distinct.
    by_name: dict[str, Block] = {}
    for block in blocks:
        previous = by_name.get(block.name)
        if previous is None or block.loc > previous.loc:
            by_name[block.name] = block
    return list(by_name.values())


def extract_blocks(path: str, text: str) -> list[Block]:
    language = language_for_path(path)
    if language is None or not text:
        return []
    if language == "python":
        return _extract_python(text)
    return _extract_braced(text)
