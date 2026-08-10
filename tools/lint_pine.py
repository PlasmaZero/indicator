#!/usr/bin/env python3
"""Static checks for the Pine source. Run: python3 lint_pine.py [file]

Pine only compiles inside TradingView, so a broken script is normally found by
pasting it in and reading a red error box. That is a slow loop and it stops at
the *first* error. These checks catch the mistakes that are mechanical enough
to find from the text alone:

  order      — Pine resolves identifiers strictly top-down, so using a variable
               above its declaration is "Undeclared identifier", not a late
               binding. This is the check that matters: a block moved for
               readability compiles fine until something in it reads a name
               defined further down.
  brackets   — unbalanced ( ) [ ] across a statement and its continuations.
  strings    — an unterminated literal, which swallows the rest of the line.
  blocks     — if/for/while whose body is not indented under it.
  dead       — top-level names assigned and never read.

Exit status is non-zero when anything in the first four groups fires; dead
names are reported as warnings only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_TARGET = Path(__file__).resolve().parent.parent / "pine" / "itm_gex_levels.pine"

# Names Pine provides. Not exhaustive — it only has to cover what the script
# uses, and anything missing shows up as a false "unknown", never a false pass.
BUILTINS = set("""
float int bool string color line label box table linefill polyline array matrix map
var varip if else for while switch to by and or not na nz true false
input indicator strategy library plot plotshape plotchar plotarrow plotcandle plotbar
bgcolor barcolor fill hline alertcondition alert log runtime
math str ta array matrix map request syminfo timeframe barstate ticker chart
time time_close time_tradingday timenow year month weekofyear dayofmonth dayofweek
hour minute second close open high low volume hlc3 ohlc4 hl2 hlcc4
bar_index last_bar_index last_bar_time size shape location display position
xloc yloc extend barmerge session adjustment format scale text order
dividends earnings currency method type enum const simple series void export import
""".split())


def strip_code(line: str) -> str:
    """Blank out string literals and trailing comments, keeping column offsets."""
    out: list[str] = []
    i = 0
    in_str = False
    quote = ""
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\":
                out.append("  ")
                i += 2
                continue
            if ch == quote:
                in_str = False
            out.append(" ")
        else:
            if ch in "\"'":
                in_str = True
                quote = ch
                out.append(" ")
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out)


DECL = re.compile(
    r"^(?:var|varip)?\s*"
    r"(?:(?:float|int|bool|string|color|line|label|box|table|linefill|polyline)(?:\[\])?"
    r"|array<[^>]*>|matrix<[^>]*>|map<[^>]*>)?\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|=)(?!=)"
)
TUPLE_DECL = re.compile(r"^\[([^\]]+)\]\s*=(?!=)")
FUNC_DECL = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*=>")
IDENT = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\b")


def top_level_defs(code: list[str]) -> dict[str, int]:
    """First line where each top-level (column 0) name is introduced."""
    defs: dict[str, int] = {}
    for n, raw in enumerate(code, 1):
        if not raw.strip() or raw[:1].isspace():
            continue
        s = raw.strip()
        for m in (FUNC_DECL.match(s), DECL.match(s)):
            if m:
                defs.setdefault(m.group(1), n)
                break
        t = TUPLE_DECL.match(s)
        if t:
            for name in t.group(1).split(","):
                defs.setdefault(name.strip().split()[-1], n)
    return defs


def local_names(code: list[str]) -> set[str]:
    """Every name bound anywhere: locals, params, loop counters."""
    names: set[str] = set()
    for raw in code:
        s = raw.strip()
        m = DECL.match(s)
        if m:
            names.add(m.group(1))
        t = TUPLE_DECL.match(s)
        if t:
            names.update(x.strip().split()[-1] for x in t.group(1).split(","))
        f = FUNC_DECL.match(s)
        if f:
            names.add(f.group(1))
            for p in f.group(2).split(","):
                p = p.strip()
                if p:
                    names.add(p.split()[-1].split("=")[0].strip())
        loop = re.match(r"^for\s+\[?([A-Za-z0-9_, ]+)\]?", s)
        if loop:
            names.update(x.strip() for x in loop.group(1).split(",") if x.strip())
    return names


def check_order(code: list[str], lines: list[str]) -> list[str]:
    defs = top_level_defs(code)
    errs, seen = [], set()
    for n, raw in enumerate(code, 1):
        for m in IDENT.finditer(raw):
            name = m.group(1)
            if name in BUILTINS or name in seen:
                continue
            first = defs.get(name)
            if first is not None and first > n:
                seen.add(name)
                errs.append(
                    f"L{n}: '{name}' is used before it is declared on L{first} "
                    f"— Pine resolves top-down\n      {lines[n - 1].strip()[:100]}"
                )
    return errs


def check_brackets(code: list[str]) -> list[str]:
    errs, depth, start = [], 0, None
    for n, raw in enumerate(code, 1):
        if depth == 0 and raw.strip():
            start = n
        depth += raw.count("(") + raw.count("[") - raw.count(")") - raw.count("]")
        if depth < 0:
            errs.append(f"L{n}: closes a bracket that was never opened")
            depth = 0
    if depth:
        errs.append(f"EOF: {depth} bracket(s) still open, statement began on L{start}")
    return errs


def check_strings(lines: list[str]) -> list[str]:
    errs = []
    for n, raw in enumerate(lines, 1):
        if raw.split("//")[0].count('"') % 2:
            errs.append(f"L{n}: unterminated string literal — {raw.strip()[:80]}")
    return errs


def check_blocks(code: list[str], lines: list[str]) -> list[str]:
    errs = []
    for i, raw in enumerate(code):
        s = raw.rstrip()
        st = s.strip()
        if not re.match(r"^(if|for|while)\b|^else\b", st):
            continue
        indent = len(s) - len(s.lstrip())
        j = i + 1
        while j < len(code) and not code[j].strip():
            j += 1
        if j >= len(code):
            errs.append(f"L{i + 1}: block header is the last line in the file")
            continue
        nxt = code[j]
        if len(nxt) - len(nxt.lstrip()) <= indent:
            errs.append(
                f"L{i + 1}: '{st[:40]}' has no indented body "
                f"(next code is L{j + 1}: {lines[j].strip()[:60]})"
            )
    return errs


def check_dead(code: list[str], lines: list[str]) -> list[str]:
    warns = []
    for name, decl in top_level_defs(code).items():
        used = any(
            n != decl and re.search(rf"(?<![\w.]){re.escape(name)}\b", raw)
            for n, raw in enumerate(code, 1)
        )
        if not used:
            warns.append(f"L{decl}: '{name}' is assigned but never read "
                         f"— {lines[decl - 1].strip()[:70]}")
    return warns


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    if not target.exists():
        print(f"no such file: {target}")
        return 2

    lines = target.read_text().split("\n")
    code = [strip_code(l) for l in lines]

    groups = [
        ("declaration order", check_order(code, lines)),
        ("brackets", check_brackets(code)),
        ("strings", check_strings(lines)),
        ("block structure", check_blocks(code, lines)),
    ]
    warnings = check_dead(code, lines)

    print(f"linting {target}  ({len(lines)} lines)\n")
    failed = 0
    for label, errs in groups:
        if errs:
            failed += len(errs)
            print(f"  {label}: {len(errs)} error(s)")
            for e in errs:
                print(f"    {e}")
        else:
            print(f"  ok  {label}")

    if warnings:
        print(f"\n  {len(warnings)} warning(s)")
        for w in warnings:
            print(f"    {w}")

    print()
    if failed:
        print(f"{failed} error(s)")
        return 1
    print("no errors")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
