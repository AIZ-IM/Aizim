"""Best-effort discovery for projects that have not built a compiler index yet."""

from __future__ import annotations

import re
from bisect import bisect_right

from aizim.domain.serialization import JsonValue

_NAME = r"(?:«[^»\n]+»|[^\W\d][\w']*)(?:\.(?:«[^»\n]+»|[^\W\d][\w']*))*"
_COMMAND = re.compile(
    r"(?m)^[ \t]*(?:@\[[^\]]*\]\s*)*"
    r"(?P<modifiers>(?:(?:private|protected|noncomputable|unsafe|partial|nonrec|local|scoped)\s+)*)"
    r"(?P<kind>namespace|section|end|theorem|lemma|def|abbrev|opaque|axiom|instance|"
    r"structure|inductive|class)\b"
)
_IDENTIFIER = re.compile(_NAME)


def mask_comments(source: str) -> tuple[str, list[tuple[int, str]]]:
    """Preserve offsets while excluding comments and string literals from discovery."""
    chars, docs, i = list(source), [], 0
    while i < len(source):
        start = i
        if source.startswith("/-", i):
            depth, i = 1, i + 2
            while i < len(source) and depth:
                if source.startswith("/-", i):
                    depth, i = depth + 1, i + 2
                elif source.startswith("-/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if source.startswith("/--", start) and depth == 0:
                docs.append((i, source[start + 3 : i - 2].strip()))
        elif source.startswith("--", i):
            i = source.find("\n", i)
            if i < 0:
                i = len(source)
        elif source[i] == '"':
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        else:
            i += 1
            continue
        for j in range(start, min(i, len(source))):
            if chars[j] != "\n":
                chars[j] = " "
    return "".join(chars), docs


def _signature_end(masked: str, start: int, end: int) -> int:
    depth = 0
    for match in re.finditer(r":=|\bwhere\b|[(){}\[\]]", masked[start:end]):
        token = match.group()
        if token in {"(", "{", "["}:
            depth += 1
        elif token in {")", "}", "]"}:
            depth = max(0, depth - 1)
        elif depth == 0:
            return start + match.start()
    return end


def extract_source(
    source: str, *, module: str, file: str, library: str
) -> list[dict[str, JsonValue]]:
    masked, docs = mask_comments(source)
    matches = list(_COMMAND.finditer(masked))
    scopes: list[str] = [""]
    newlines = [i for i, char in enumerate(source) if char == "\n"]
    result: list[dict[str, JsonValue]] = []
    for index, match in enumerate(matches):
        kind = match["kind"]
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        tail = masked[match.end() : end]
        name_match = _IDENTIFIER.match(tail.lstrip())
        name = name_match.group() if name_match else ""
        if kind == "end":
            if len(scopes) > 1:
                scopes.pop()
            continue
        if kind in {"namespace", "section"}:
            scopes.append(
                ".".join(filter(None, (scopes[-1], name))) if kind == "namespace" else scopes[-1]
            )
            continue
        if not name or "private" in match["modifiers"].split():
            continue
        # Anonymous instances and class-inductive syntax need the compiler backend.
        if name in {"where", "inductive"}:
            continue
        qualified = ".".join(filter(None, (scopes[-1], name)))
        if name.startswith("_root_."):
            qualified = name.removeprefix("_root_.")
        docstring = ""
        for position, body in reversed(docs):
            if position <= match.start():
                if not masked[position : match.start()].strip():
                    docstring = body
                break
        signature_end = _signature_end(masked, match.end(), end)
        result.append(
            {
                "name": name,
                "full_name": qualified,
                "kind": kind,
                "signature": source[match.start() : signature_end].strip()[:8192],
                "docstring": docstring,
                "module": module,
                "file": file,
                "line": bisect_right(newlines, match.start()) + 1,
                "library": library,
                "origin": "source",
            }
        )
    return result
