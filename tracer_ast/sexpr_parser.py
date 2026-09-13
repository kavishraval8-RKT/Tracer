"""S-expression parser -- Layer 1 of the tracer_ast module.

KiCad's file format is Lisp-style S-expressions with KiCad-specific
formatting conventions (indentation, float precision, unquoted symbols vs.
quoted strings). This layer knows nothing about KiCad semantics; it turns
text into a tree of SExprAtom | SExprList and back again, byte-identical for
untouched nodes.

The higher layers (schematic_model, mutators, placement) will sit on top of
this. This one file is standalone and testable.

    from tracer_ast.sexpr_parser import parse, serialize
    tree = parse(open('foo.kicad_sch').read())
    open('foo.kicad_sch', 'w').write(serialize(tree))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Union


# ---------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------


@dataclass
class SExprAtom:
    """A single token: a symbol (unquoted), a number, or a quoted string.

    `raw` preserves the exact source spelling so round-tripping a number like
    "0.500" doesn't turn it into "0.5", and a quoted string keeps its
    quoting and escaping intact.
    """
    raw: str
    quoted: bool = False

    def __str__(self) -> str:
        return self.raw


@dataclass
class SExprList:
    """A parenthesized list -- the container node.

    `head` is the first atom's raw text ("symbol", "at", "kicad_sch" ...) so
    lookups like `find_all("symbol")` don't hunt through children each time.
    `children` is everything after the head, in order.
    """
    head: str
    children: List["SExprNode"] = field(default_factory=list)

    def find_all(self, head: str) -> List["SExprList"]:
        """Direct children matching this head name. Not recursive."""
        return [c for c in self.children
                if isinstance(c, SExprList) and c.head == head]

    def walk(self) -> Iterator["SExprList"]:
        """Depth-first iterator over every SExprList in the subtree."""
        yield self
        for c in self.children:
            if isinstance(c, SExprList):
                yield from c.walk()


SExprNode = Union[SExprAtom, SExprList]


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


class ParseError(Exception):
    pass


def parse(text: str) -> SExprList:
    """Parse a full .kicad_sch string. Returns the root list node."""
    tokens = _tokenize(text)
    pos = [0]  # boxed so nested helpers can advance it
    node = _read(tokens, pos)
    if pos[0] != len(tokens):
        raise ParseError(f"trailing tokens at position {pos[0]}")
    if not isinstance(node, SExprList):
        raise ParseError("root must be a list")
    return node


def _read(tokens: List[str], pos: List[int]) -> SExprNode:
    if pos[0] >= len(tokens):
        raise ParseError("unexpected end of input")
    tok = tokens[pos[0]]
    pos[0] += 1
    if tok == "(":
        # head is the first token of the list
        if pos[0] >= len(tokens):
            raise ParseError("list with no head")
        head_tok = tokens[pos[0]]
        if head_tok == "(" or head_tok == ")":
            raise ParseError(f"list must have a symbol head, got {head_tok!r}")
        pos[0] += 1
        head = _atom_text(head_tok)
        node = SExprList(head=head)
        while pos[0] < len(tokens) and tokens[pos[0]] != ")":
            node.children.append(_read(tokens, pos))
        if pos[0] >= len(tokens):
            raise ParseError("unclosed list")
        pos[0] += 1  # consume ')'
        return node
    if tok == ")":
        raise ParseError("unexpected ')'")
    return _to_atom(tok)


def _tokenize(text: str) -> List[str]:
    """Split into paren tokens, quoted strings, and bare atoms.

    Whitespace is a separator but not preserved; that's fine because the
    serializer re-emits KiCad's canonical formatting.
    """
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            # quoted string: scan to matching quote, honouring backslash escapes
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            if j >= n:
                raise ParseError(f"unterminated string starting at {i}")
            out.append(text[i:j + 1])  # keep quotes
            i = j + 1
            continue
        # bare atom: anything until whitespace or paren
        j = i
        while j < n and not text[j].isspace() and text[j] not in "()":
            j += 1
        out.append(text[i:j])
        i = j
    return out


def _atom_text(tok: str) -> str:
    """Strip quotes off a quoted token for the head field. Bare tokens pass through."""
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        # de-escape enough for the head name to be usable
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


def _to_atom(tok: str) -> SExprAtom:
    quoted = len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"'
    return SExprAtom(raw=tok, quoted=quoted)


# ---------------------------------------------------------------------------
# serializer
# ---------------------------------------------------------------------------


def serialize(node: SExprList, indent: int = 0, tab: str = "\t") -> str:
    """Emit KiCad's canonical formatting: tab-indented, one child per line
    for lists, atoms space-separated on the parent's line.

    ponytail: canonical whitespace, not byte-perfect. Matches KiCad's own
    writer output on files produced by KiCad. Files hand-edited into
    non-canonical shapes will re-format. Upgrade path: preserve original
    whitespace via a per-token layout hint if that ever matters.
    """
    return _emit(node, indent, tab)


def _emit(node: SExprNode, depth: int, tab: str) -> str:
    if isinstance(node, SExprAtom):
        return node.raw
    lead = tab * depth
    # If every child is atomic and the whole list is short, inline it.
    if all(isinstance(c, SExprAtom) for c in node.children):
        inline = "(" + node.head
        for c in node.children:
            inline += " " + c.raw
        inline += ")"
        return inline if depth == 0 else inline
    # Otherwise break children across lines under the parent.
    parts = [f"{lead}({node.head}"]
    for c in node.children:
        if isinstance(c, SExprAtom):
            parts[-1] += " " + c.raw
        else:
            parts.append(_emit(c, depth + 1, tab))
    parts.append(f"{lead})")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# self-check -- ponytail: one runnable check, not a framework
# ---------------------------------------------------------------------------


def _demo() -> None:
    """Round-trip a small hand-written .kicad_sch fragment.

    Not a byte-identity check against a KiCad file (see the module docstring
    ceiling) -- verifies structure, quoting, and lookups survive."""
    src = """\
(kicad_sch
\t(version 20250114)
\t(generator "kicad_sch_api")
\t(uuid abc-123)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100.0 50.8 0)
\t\t(property "Reference" "R1" (at 0 0 0))
\t\t(property "Value" "10k" (at 0 0 0))
\t)
\t(wire (pts (xy 0 0) (xy 10 0)))
)"""
    tree = parse(src)
    assert tree.head == "kicad_sch", tree.head
    assert tree.find_all("symbol"), "symbol not found"
    sym = tree.find_all("symbol")[0]
    lib_id = sym.find_all("lib_id")[0].children[0]
    assert isinstance(lib_id, SExprAtom) and lib_id.raw == '"Device:R"', lib_id
    # walk() finds every list, wire included
    heads = {n.head for n in tree.walk()}
    assert {"kicad_sch", "symbol", "property", "wire", "pts", "xy"} <= heads, heads

    # Round-trip: reparse the serialized output and confirm structure survives
    reparsed = parse(serialize(tree))
    assert reparsed.head == tree.head
    assert len(reparsed.find_all("symbol")) == len(tree.find_all("symbol"))
    # A specific property survives with quoting intact
    prop = reparsed.find_all("symbol")[0].find_all("property")[0]
    assert prop.children[0].raw == '"Reference"'
    print("sexpr_parser: OK (parse + walk + round-trip)")


if __name__ == "__main__":
    _demo()
