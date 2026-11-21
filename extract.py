"""Read source files with tree-sitter into one record per file: size, functions (complexity, parameters),
classes (bases, members), imports, the identifiers it uses, magic values, error handlers that swallow, and a
normalised token stream for clone detection. Language-agnostic above the per-language tables below."""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter_language_pack import get_parser

LANGS = {
    ".py": "python",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".h": "cpp",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}

# per language: which node types are functions, classes, imports, decisions, literals, catch handlers
FUNCS = {
    "python": {"function_definition"},
    "csharp": {"method_declaration", "constructor_declaration", "local_function_statement", "operator_declaration"},
    "cpp": {"function_definition"},
    "typescript": {"function_declaration", "method_definition", "arrow_function", "function_expression"},
    "tsx": {"function_declaration", "method_definition", "arrow_function", "function_expression"},
    "javascript": {"function_declaration", "method_definition", "arrow_function", "function_expression"},
}
CLASSES = {
    "python": {"class_definition"},
    "csharp": {
        "class_declaration",
        "struct_declaration",
        "interface_declaration",
        "record_declaration",
        "enum_declaration",
    },
    "cpp": {"class_specifier", "struct_specifier"},
    "typescript": {"class_declaration", "interface_declaration", "abstract_class_declaration"},
    "tsx": {"class_declaration", "interface_declaration", "abstract_class_declaration"},
    "javascript": {"class_declaration"},
}
DECISIONS = {
    "if_statement",
    "elif_clause",
    "for_statement",
    "for_in_statement",
    "foreach_statement",
    "for_range_loop",
    "while_statement",
    "do_statement",
    "except_clause",
    "catch_clause",
    "case_clause",
    "switch_section",
    "case_statement",
    "switch_case",
    "conditional_expression",
    "ternary_expression",
    "for_in_clause",
    "if_clause",
    "conditional_access_expression",
}
BOOL_OPS = {"and", "or", "&&", "||", "??"}
NUMBERS = {"integer", "float", "integer_literal", "real_literal", "number_literal", "number"}
STRINGS = {
    "string",
    "string_literal",
    "verbatim_string_literal",
    "raw_string_literal",
    "template_string",
    "interpolated_string_expression",
}
COMMENTS = {"comment", "line_comment", "block_comment"}
CATCHES = {"except_clause", "catch_clause"}
TRIVIAL_NUMBERS = {"0", "1", "-1", "2", "0.0", "1.0", "0.5", "10", "100", "1000", "0f", "1f", "0.0f", "1.0f", "2.0"}
URL = re.compile(
    r"^[a-z]+://|^\d{1,3}(\.\d{1,3}){3}(:\d+)?$|^[a-z0-9.-]+:\d{2,5}$|^[A-Za-z]:\\|^/(usr|etc|tmp|var|home|Users|opt)/|^~/"
)


@dataclass
class Function:
    name: str
    line: int
    lines: int
    complexity: int
    params: int


@dataclass
class Klass:
    name: str
    line: int
    lines: int
    bases: list[str]
    methods: list[str]
    fields: list[str]


@dataclass
class FileRecord:
    path: str
    lang: str
    loc: int  # non-blank, non-comment lines
    lines: int
    functions: list[Function] = field(default_factory=list)
    classes: list[Klass] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    defines: set[str] = field(default_factory=set)  # top-level names others can refer to
    uses: set[str] = field(default_factory=set)  # identifiers used
    magic: list[tuple[int, str, str]] = field(default_factory=list)  # (line, kind, text)
    swallowed: list[int] = field(default_factory=list)  # lines of handlers that swallow errors
    namespace: str = ""
    tokens: list[int] = field(default_factory=list)  # hashed normalised tokens, for clones
    token_lines: list[int] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)  # called names (for layer detection)
    strings: list[str] = field(default_factory=list)  # short string literals (for layer detection)
    code: str = ""  # the source with comments and docstrings blanked out (for layer detection)


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _name(node, src: bytes) -> str:
    n = node.child_by_field_name("name")
    if n is not None:
        return _text(n, src)
    if node.type == "function_definition":  # C++: declarator chain
        d = node.child_by_field_name("declarator")
        while d is not None and d.child_by_field_name("declarator") is not None and d.type != "qualified_identifier":
            d = d.child_by_field_name("declarator")
        if d is not None:
            return _text(d, src).split("(")[0][-80:]
    if node.type in ("arrow_function", "function_expression") and node.parent is not None:
        p = node.parent
        if p.type in ("variable_declarator", "pair", "assignment_expression", "public_field_definition"):
            k = p.child_by_field_name("name") or p.child_by_field_name("key") or p.child_by_field_name("left")
            if k is not None:
                return _text(k, src)
    return "<anonymous>"


def _params(node) -> int:
    p = node.child_by_field_name("parameters")
    if p is None:
        d = node.child_by_field_name("declarator")
        while d is not None and p is None:
            p = d.child_by_field_name("parameters")
            d = d.child_by_field_name("declarator")
    if p is None:
        return 0
    return sum(1 for c in p.named_children if "comment" not in c.type and c.type not in ("self",))


def _complexity(node) -> int:
    n, stack = 1, [node]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if c.type in FUNCS_ALL and c is not node:
                continue  # nested functions count on their own
            if c.type in DECISIONS:
                n += 1
            elif c.type in ("boolean_operator", "binary_expression"):
                op = c.child_by_field_name("operator")
                if op is not None and op.type in BOOL_OPS:
                    n += 1
            stack.append(c)
    return n


FUNCS_ALL = set().union(*FUNCS.values())


def _swallows(node, src: bytes, lang: str) -> bool:
    """A handler that catches everything (or Exception) and does nothing visible with it."""
    t = _text(node, src)
    head = t.split(":", 1)[0] if lang == "python" else t.split("{", 1)[0]
    broad = (
        head.strip() in ("except", "catch")
        or re.search(r"\b(Exception|BaseException|std::exception|\.\.\.|Error)\b", head) is not None
        or (lang in ("typescript", "tsx", "javascript") and True)
    )
    body = node.child_by_field_name("body") or (node.named_children[-1] if node.named_children else None)
    if body is None:
        return False
    stmts = [c for c in body.named_children if c.type not in COMMENTS]
    if not stmts:
        return broad
    if len(stmts) == 1:
        s = _text(stmts[0], src).strip()
        if s in (
            "pass",
            "continue",
            "break",
            "return",
            "return;",
            "continue;",
            "break;",
            "return null;",
            "return None",
            "return false;",
            "return False",
            "{}",
            ";",
        ):
            return broad
    return False


def read(path: Path, rel: str) -> FileRecord | None:
    lang = LANGS.get(path.suffix)
    if lang is None:
        return None
    src = path.read_bytes()
    try:
        tree = get_parser(lang).parse(src)
    except Exception:  # noqa: BLE001 - a file the grammar cannot read is skipped, not fatal
        return None
    text = src.decode("utf-8", "replace")
    rec = FileRecord(path=rel, lang=lang, loc=0, lines=text.count("\n") + 1)
    comment_lines: set[int] = set()
    prose: list[tuple[int, int]] = []  # byte spans of comments and docstrings
    stack = [tree.root_node]
    depth_of_class = 0
    while stack:
        node = stack.pop()
        t = node.type
        if t in COMMENTS:
            for ln in range(node.start_point[0], node.end_point[0] + 1):
                comment_lines.add(ln)
            prose.append((node.start_byte, node.end_byte))
            continue
        if (
            t == "expression_statement" and node.named_child_count == 1 and node.named_children[0].type == "string"
        ) or (t == "string" and node.parent is not None and node.parent.type in ("module", "block")):
            prose.append((node.start_byte, node.end_byte))  # a docstring (both forms the grammar produces)
        if t in FUNCS[lang] if lang in FUNCS else False:
            name = _name(node, src)
            rec.functions.append(
                Function(
                    name,
                    node.start_point[0] + 1,
                    node.end_point[0] - node.start_point[0] + 1,
                    _complexity(node),
                    _params(node),
                )
            )
            if node.parent is not None and node.parent.type in (
                "module",
                "translation_unit",
                "program",
                "namespace_declaration",
                "declaration_list",
                "file_scoped_namespace_declaration",
                "export_statement",
                "compilation_unit",
            ):
                rec.defines.add(name.split("::")[-1])
        elif t in CLASSES.get(lang, ()):
            name = _name(node, src)
            bases = []
            for c in node.children:
                if c.type in (
                    "argument_list",
                    "base_list",
                    "base_class_clause",
                    "class_heritage",
                    "superclasses",
                    "extends_clause",
                    "implements_clause",
                ):
                    bases += [
                        b
                        for b in re.findall(r"[A-Za-z_][\w.:<>]*", _text(c, src))
                        if b not in ("public", "private", "protected", "extends", "implements", "virtual", "metaclass")
                    ]
            body = node.child_by_field_name("body")
            methods, fields = [], []
            if body is not None:
                for c in body.named_children:
                    if c.type in FUNCS_ALL or c.type in ("decorated_definition",):
                        inner = c.child_by_field_name("definition") if c.type == "decorated_definition" else c
                        if inner is not None:
                            methods.append(_name(inner, src))
                    elif c.type in (
                        "field_declaration",
                        "property_declaration",
                        "public_field_definition",
                        "expression_statement",
                        "field_definition",
                    ):
                        m = re.findall(r"([A-Za-z_]\w*)\s*(?::|=|;|\{)", _text(c, src))
                        if m:
                            fields.append(m[0])
            rec.classes.append(
                Klass(
                    name,
                    node.start_point[0] + 1,
                    node.end_point[0] - node.start_point[0] + 1,
                    bases[:6],
                    methods[:60],
                    fields[:40],
                )
            )
            rec.defines.add(name)
            depth_of_class += 1
        elif t in ("import_statement", "import_from_statement", "using_directive", "preproc_include"):
            rec.imports.append(_text(node, src))
        elif t in ("namespace_declaration", "file_scoped_namespace_declaration") and not rec.namespace:
            n = node.child_by_field_name("name")
            if n is not None:
                rec.namespace = _text(n, src)
        elif t in CATCHES:
            if _swallows(node, src, lang):
                rec.swallowed.append(node.start_point[0] + 1)
        elif t in NUMBERS:
            v = _text(node, src)
            p = node.parent
            if v not in TRIVIAL_NUMBERS and not _named_constant(p, src) and not _in_index(p):
                rec.magic.append((node.start_point[0] + 1, "number", v))
            continue
        elif t in STRINGS:
            v = _text(node, src).strip("\"'`@$fbrBRuU")
            if len(v) <= 80:
                rec.strings.append(v)
            if URL.search(v):
                rec.magic.append((node.start_point[0] + 1, "address", v[:80]))
            continue
        elif t in ("identifier", "type_identifier", "property_identifier", "field_identifier", "qualified_identifier"):
            rec.uses.add(_text(node, src).split("::")[-1].split(".")[-1])
        elif t in ("call", "invocation_expression", "call_expression"):
            f = node.child_by_field_name("function") or (node.named_children[0] if node.named_children else None)
            if f is not None:
                rec.calls.append(_text(f, src)[:80])
        if (
            lang == "python"
            and t == "assignment"
            and node.parent is not None
            and node.parent.parent is not None
            and node.parent.parent.type == "module"
        ):
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                rec.defines.add(_text(left, src))
        stack.extend(reversed(node.children))
    code_lines = {i for i, ln in enumerate(text.split("\n")) if ln.strip()}
    rec.loc = len(code_lines - comment_lines)
    blank = bytearray(src)
    for a, b in prose:
        blank[a:b] = b" " * (b - a)
    rec.code = blank.decode("utf-8", "replace")
    _tokens(tree.root_node, src, rec)
    return rec


def _named_constant(p, src: bytes) -> bool:
    """`MAX_RETRIES = 3`, `const int Port = 8000`, an enum member: a literal that has a name is not magic."""
    for _ in range(3):
        if p is None:
            return False
        if p.type in (
            "assignment",
            "variable_declarator",
            "enum_member_declaration",
            "enumerator",
            "keyword_argument",
            "default_parameter",
            "typed_default_parameter",
            "pair",
            "equals_value_clause",
            "init_declarator",
        ):
            target = p.child_by_field_name("left") or p.child_by_field_name("name") or p.child_by_field_name("key")
            if target is not None:
                name = _text(target, src)
                if (
                    name.isupper()
                    or (name[:1].isupper() and "_" not in name and p.type != "pair")
                    or p.type
                    in (
                        "enum_member_declaration",
                        "enumerator",
                        "keyword_argument",
                        "default_parameter",
                        "typed_default_parameter",
                    )
                ):
                    return True
            return False
        p = p.parent
    return False


def _in_index(p) -> bool:
    return p is not None and p.type in (
        "subscript",
        "element_access_expression",
        "subscript_expression",
        "subscript_argument_list",
        "bracketed_argument_list",
        "slice",
    )


def _tokens(root, src: bytes, rec: FileRecord) -> None:
    """Leaves as a normalised stream: identifiers -> I, literals -> L, everything else as itself; comments out."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in COMMENTS:
            continue
        if n.child_count == 0 or n.type in NUMBERS or n.type in STRINGS:
            if n.type in NUMBERS or n.type in STRINGS:
                tok = "L"
            elif "identifier" in n.type or n.type in ("name", "this", "self"):
                tok = "I"
            else:
                tok = _text(n, src) if n.end_byte - n.start_byte < 12 else n.type
            if tok.strip():
                rec.tokens.append(zlib.crc32(tok.encode()))
                rec.token_lines.append(n.start_point[0] + 1)
            continue
        stack.extend(reversed(n.children))
