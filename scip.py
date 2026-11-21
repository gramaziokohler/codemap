"""Read SCIP indexes (https://github.com/sourcegraph/scip): what compiler-grade indexers (scip-python on Pyright,
scip-typescript on the TypeScript compiler, scip-dotnet on Roslyn, scip-clang) know about a code base: every
definition and every reference, with the symbol it resolves to, and the relationships between symbols (a class
implementing or deriving from another).

A small protobuf reader for the few fields used here, so no generated code and no dependency: scip.proto's
Index.documents (2), Document.relative_path (1), .occurrences (2), .symbols (3); Occurrence.range (1, or the typed
single_line_range 8 / multi_line_range 9), .symbol (2), .symbol_roles (3), .enclosing_range (7); SymbolInformation
.symbol (1), .relationships (4), .kind (5), .display_name (6); Relationship.symbol (1), .is_implementation (3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFINITION = 0x1
IMPORT = 0x2

# ------------------------------------------------------------------------------------- protobuf, by hand


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = out = 0
    while True:
        c = b[i]
        i += 1
        out |= (c & 0x7F) << shift
        if c < 0x80:
            return out, i
        shift += 7


def _fields(b: bytes):
    """(field number, wire type, value) of one message: an int for varints, bytes for length-delimited."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i : i + ln]
            i += ln
        elif wt == 1:
            v = b[i : i + 8]
            i += 8
        elif wt == 5:
            v = b[i : i + 4]
            i += 4
        else:
            raise ValueError(f"scip: wire type {wt} not supported")
        yield num, wt, v


def _ints(wt: int, v) -> list[int]:
    if wt == 0:
        return [v]
    out, i = [], 0
    while i < len(v):
        x, i = _varint(v, i)
        out.append(x)
    return out


def _range(fs: dict[int, list]) -> tuple[int, int] | None:
    """The start and end line (0-based) of an occurrence."""
    if 8 in fs:  # SingleLineRange
        line = dict((n, v) for n, _, v in _fields(fs[8][0])).get(1, 0)
        return line, line
    if 9 in fs:  # MultiLineRange
        d = dict((n, v) for n, _, v in _fields(fs[9][0]))
        return d.get(1, 0), d.get(3, 0)
    r = fs.get(1)
    if not r:
        return None
    return r[0], (r[2] if len(r) == 4 else r[0])


# ------------------------------------------------------------------------------------- the model


@dataclass
class Index:
    """What the indexes say, by repository path (relative to the mapped root)."""

    files: set[str] = field(default_factory=set)  # the documents an indexer covered
    defs: dict[str, tuple[str, int]] = field(default_factory=dict)  # symbol -> (path, line)
    refs: list[tuple[str, int, str, bool]] = field(default_factory=list)  # (path, line, symbol, is import)
    impl: dict[str, set[str]] = field(default_factory=dict)  # symbol -> symbols it implements or derives from
    kind: dict[str, int] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)


def read(data: bytes, prefix: str, into: Index) -> Index:
    """Add one index to ``into``; ``prefix`` is the indexed project's folder relative to the mapped root."""
    pre = (prefix.strip("/") + "/") if prefix.strip("/. ") else ""
    for num, _, doc in _fields(data):
        if num == 1:
            for n, _, v in _fields(doc):
                if n == 2:  # tool_info
                    t = dict((k, x) for k, _, x in _fields(v))
                    into.tools.append(f"{t.get(1, b'').decode()} {t.get(2, b'').decode()}".strip())
            continue
        if num != 2:
            continue
        path, occs, syms = None, [], []
        for n, _, v in _fields(doc):
            if n == 1:
                path = pre + v.decode()
            elif n == 2:
                occs.append(v)
            elif n == 3:
                syms.append(v)
        if path is None:
            continue
        into.files.add(path)
        for o in occs:
            fs: dict[int, list] = {}
            for n, wt, v in _fields(o):
                if n in (1, 7):
                    fs.setdefault(n, []).extend(_ints(wt, v))
                else:
                    fs.setdefault(n, []).append(v)
            sym = fs.get(2, [b""])[0].decode()
            if not sym or sym.startswith("local "):
                continue
            rng = _range(fs)
            if rng is None:
                continue
            roles = fs.get(3, [0])[0]
            if roles & DEFINITION:
                into.defs.setdefault(sym, (path, rng[0] + 1))
            else:
                into.refs.append((path, rng[0] + 1, sym, bool(roles & IMPORT)))
        for s in syms:
            d: dict[int, list] = {}
            for n, _, v in _fields(s):
                d.setdefault(n, []).append(v)
            sym = d.get(1, [b""])[0].decode()
            if not sym or sym.startswith("local "):
                continue
            if 5 in d:
                into.kind[sym] = d[5][0]
            for rel in d.get(4, []):
                r = dict((n, v) for n, _, v in _fields(rel))
                if r.get(3) and r.get(1):
                    into.impl.setdefault(sym, set()).add(r[1].decode())
    return into


# ------------------------------------------------------------------------------------- symbols

_DESC = re.compile(r"`[^`]*`|[^/#.()\[\]`:!]+|[/#.()\[\]:!]")


def owner_type(symbol: str) -> str | None:
    """The type a symbol belongs to, or is (`pkg/Foo#bar().` and `pkg/Foo#` -> `pkg/Foo#`), else None."""
    depth = 0
    for i, c in enumerate(symbol):
        if c == "`":
            depth ^= 1
        elif c == "#" and not depth:
            return symbol[: i + 1]
    return None


def type_name(symbol: str) -> str:
    """The short name of a type symbol (`... src/`a.ts`/Foo#` -> Foo)."""
    body = symbol.rstrip("#")
    tail = re.split(r"[/#.]", body.replace("`", ""))[-1]
    return tail or body
