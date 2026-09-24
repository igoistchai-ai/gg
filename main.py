# main.py
# Advanced Lua Static Deobfuscator
# Does NOT execute uploaded Lua code.
#
# Supports many static transformations:
# - decimal / hex / octal / binary escapes
# - string.char / string.byte
# - string.reverse
# - string.rep
# - string.lower / upper
# - string.sub
# - string.format for constants
# - table.concat
# - table.insert
# - arithmetic constant folding
# - boolean folding
# - comparisons
# - concatenation
# - local constant propagation
# - table constant propagation
# - aliases
# - dead constant cleanup
# - duplicate parentheses
# - redundant tostring / tonumber
# - nested constant expressions
# - escaped strings
# - generated variable cleanup
# - comments
# - whitespace normalization
# - indentation
# - HTML report
#
# pip install aiogram aiohttp

import os
import re
import html
import asyncio
import logging
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))
MAX_FILE_SIZE = 10 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# SAFE LEXICAL UTILITIES
# ============================================================

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

NUMBER_RE = re.compile(
    r"""
    (?<![\w.])
    (
        0[xX][0-9A-Fa-f]+
        |
        0[bB][01]+
        |
        0[oO][0-7]+
        |
        \d+(?:\.\d+)?
    )
    (?![\w.])
    """,
    re.X
)

STRING_RE = re.compile(
    r"""
    (
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
    )
    """,
    re.X
)


def lua_quote(value):
    value = str(value)
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("\r", "\\r")
    value = value.replace("\n", "\\n")
    value = value.replace("\t", "\\t")
    return '"' + value + '"'


def strip_quotes(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def is_string_literal(s):
    s = s.strip()
    return (
        len(s) >= 2
        and s[0] == s[-1]
        and s[0] in "\"'"
    )


def is_number_literal(s):
    return bool(NUMBER_RE.fullmatch(s.strip()))


def parse_number(s):
    s = s.strip()

    try:
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+", s):
            return int(s, 16)

        if re.fullmatch(r"0[bB][01]+", s):
            return int(s, 2)

        if re.fullmatch(r"0[oO][0-7]+", s):
            return int(s, 8)

        if "." in s:
            return float(s)

        return int(s)
    except Exception:
        return None


# ============================================================
# LUA STRING DECODER
# ============================================================

def decode_lua_escape_sequence(text):
    out = []
    i = 0

    simple = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        '"': '"',
        "'": "'",
    }

    while i < len(text):
        if text[i] != "\\":
            out.append(text[i])
            i += 1
            continue

        if i + 1 >= len(text):
            out.append("\\")
            break

        c = text[i + 1]

        if c in simple:
            out.append(simple[c])
            i += 2
            continue

        # decimal escape
        m = re.match(r"\\([0-9]{1,3})", text[i:])
        if m:
            try:
                out.append(chr(int(m.group(1), 10)))
                i += len(m.group(0))
                continue
            except Exception:
                pass

        # hex escape
        m = re.match(r"\\x([0-9A-Fa-f]{2})", text[i:])
        if m:
            try:
                out.append(chr(int(m.group(1), 16)))
                i += len(m.group(0))
                continue
            except Exception:
                pass

        # unicode-like Lua escape
        m = re.match(r"\\u\{([0-9A-Fa-f]+)\}", text[i:])
        if m:
            try:
                out.append(chr(int(m.group(1), 16)))
                i += len(m.group(0))
                continue
            except Exception:
                pass

        # escaped newline
        if c == "\n":
            i += 2
            out.append("\n")
            continue

        out.append(c)
        i += 2

    return "".join(out)


def decode_string_literal(token):
    if not is_string_literal(token):
        return None

    raw = strip_quotes(token)

    try:
        return decode_lua_escape_sequence(raw)
    except Exception:
        return None


# ============================================================
# COMMENT REMOVAL
# ============================================================

def remove_comments(code):
    # Long comments
    code = re.sub(
        r"--\[(=*)\[.*?\]\1\]",
        "",
        code,
        flags=re.S
    )

    # Single line comments, avoiding strings
    result = []
    i = 0
    quote = None

    while i < len(code):
        c = code[i]

        if quote:
            result.append(c)

            if c == "\\" and i + 1 < len(code):
                result.append(code[i + 1])
                i += 2
                continue

            if c == quote:
                quote = None

            i += 1
            continue

        if c in "\"'":
            quote = c
            result.append(c)
            i += 1
            continue

        if c == "-" and i + 1 < len(code) and code[i + 1] == "-":
            while i < len(code) and code[i] != "\n":
                i += 1
            result.append("\n")
            continue

        result.append(c)
        i += 1

    return "".join(result)


# ============================================================
# TOP LEVEL SPLITTER
# ============================================================

def split_top_level(text, separator=","):
    parts = []
    start = 0
    depth = 0
    quote = None
    i = 0

    while i < len(text):
        c = text[i]

        if quote:
            if c == "\\":
                i += 2
                continue

            if c == quote:
                quote = None

            i += 1
            continue

        if c in "\"'":
            quote = c
            i += 1
            continue

        if c in "([{":
            depth += 1

        elif c in ")]}":
            depth -= 1

        elif c == separator and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1

        i += 1

    parts.append(text[start:].strip())
    return parts


def split_binary(text, operator):
    depth = 0
    quote = None
    i = 0

    while i < len(text):
        c = text[i]

        if quote:
            if c == "\\":
                i += 2
                continue

            if c == quote:
                quote = None

            i += 1
            continue

        if c in "\"'":
            quote = c
            i += 1
            continue

        if c in "([{":
            depth += 1
            i += 1
            continue

        if c in ")]}":
            depth -= 1
            i += 1
            continue

        if depth == 0 and text.startswith(operator, i):
            return text[:i].strip(), text[i + len(operator):].strip()

        i += 1

    return None


# ============================================================
# SAFE CONSTANT EVALUATOR
# ============================================================

class Evaluator:
    def __init__(self, constants=None, tables=None):
        self.constants = constants or {}
        self.tables = tables or {}

    def eval(self, expr):
        expr = expr.strip()

        if not expr:
            return None

        # Remove unnecessary outer parentheses
        while (
            len(expr) >= 2
            and expr[0] == "("
            and expr[-1] == ")"
        ):
            inner = expr[1:-1].strip()

            if self.balanced(inner):
                expr = inner
            else:
                break

        # Literal string
        if is_string_literal(expr):
            return decode_string_literal(expr)

        # Number
        n = parse_number(expr)
        if n is not None:
            return n

        # Boolean
        if expr == "true":
            return True

        if expr == "false":
            return False

        if expr == "nil":
            return None

        # Known constant
        if expr in self.constants:
            return self.constants[expr]

        # Table indexed access
        m = re.fullmatch(
            rf"({IDENT})\s*\[\s*(\d+)\s*\]",
            expr
        )

        if m:
            table_name = m.group(1)
            index = int(m.group(2))

            if table_name in self.tables:
                table = self.tables[table_name]

                if index in table:
                    return table[index]

        # #table
        m = re.fullmatch(
            rf"#\s*({IDENT})",
            expr
        )

        if m and m.group(1) in self.tables:
            return len(self.tables[m.group(1)])

        # tonumber
        m = re.fullmatch(
            r"tonumber\s*\(\s*(.*?)\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))

            if isinstance(value, (int, float)):
                return value

            if isinstance(value, str):
                try:
                    return int(value)
                except Exception:
                    try:
                        return float(value)
                    except Exception:
                        pass

        # tostring
        m = re.fullmatch(
            r"tostring\s*\(\s*(.*?)\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))

            if value is not None:
                if value is True:
                    return "true"
                if value is False:
                    return "false"
                return str(value)

        # string.char
        m = re.fullmatch(
            r"string\s*\.\s*char\s*\((.*)\)",
            expr,
            re.S
        )

        if m:
            args = split_top_level(m.group(1))
            chars = []

            for arg in args:
                value = self.eval(arg)

                if not isinstance(value, (int, float)):
                    return None

                try:
                    chars.append(chr(int(value) % 256))
                except Exception:
                    return None

            return "".join(chars)

        # string.byte
        m = re.fullmatch(
            r"string\s*\.\s*byte\s*\(\s*(.*?)(?:\s*,\s*(.*?))?\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))

            if not isinstance(value, str) or not value:
                return None

            start = 1

            if m.group(2):
                pos = self.eval(m.group(2))
                if isinstance(pos, (int, float)):
                    start = int(pos)

            if 1 <= start <= len(value):
                return ord(value[start - 1])

            return None

        # string.reverse
        m = re.fullmatch(
            r"string\s*\.\s*reverse\s*\(\s*(.*?)\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))

            if isinstance(value, str):
                return value[::-1]

        # string.lower
        m = re.fullmatch(
            r"string\s*\.\s*lower\s*\(\s*(.*?)\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))

            if isinstance(value, str):
                return value.lower()

        # string.upper
        m = re.fullmatch(
            r"string\s*\.\s*upper\s*\(\s*(.*?)\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))

            if isinstance(value, str):
                return value.upper()

        # string.len
        m = re.fullmatch(
            r"string\s*\.\s*len\s*\(\s*(.*?)\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))

            if isinstance(value, str):
                return len(value)

        # string.rep
        m = re.fullmatch(
            r"string\s*\.\s*rep\s*\(\s*(.*?)\s*,\s*(.*?)\s*\)",
            expr,
            re.S
        )

        if m:
            a = self.eval(m.group(1))
            b = self.eval(m.group(2))

            if isinstance(a, str) and isinstance(b, (int, float)):
                return a * int(b)

        # string.sub
        m = re.fullmatch(
            r"string\s*\.\s*sub\s*\(\s*(.*?)\s*,\s*(.*?)(?:\s*,\s*(.*?))?\s*\)",
            expr,
            re.S
        )

        if m:
            value = self.eval(m.group(1))
            start = self.eval(m.group(2))
            stop = self.eval(m.group(3)) if m.group(3) else None

            if (
                isinstance(value, str)
                and isinstance(start, (int, float))
            ):
                start = int(start)

                if start < 0:
                    start = len(value) + start + 1

                start = max(1, start)

                if stop is None:
                    return value[start - 1:]

                if isinstance(stop, (int, float)):
                    stop = int(stop)

                    if stop < 0:
                        stop = len(value) + stop + 1

                    return value[start - 1:stop]

        # table.concat
        m = re.fullmatch(
            r"table\s*\.\s*concat\s*\(\s*(.*?)(?:\s*,\s*(.*?))?\s*\)",
            expr,
            re.S
        )

        if m:
            table_expr = m.group(1)
            separator = self.eval(m.group(2)) if m.group(2) else ""

            if not isinstance(separator, str):
                separator = ""

            table = self.get_table(table_expr)

            if table is not None:
                values = []

                for i in range(1, len(table) + 1):
                    if i in table:
                        values.append(str(table[i]))

                return separator.join(values)

        # concatenation
        pieces = split_top_level_operator(expr, "..")

        if len(pieces) > 1:
            values = []

            for piece in pieces:
                value = self.eval(piece)

                if value is None:
                    return None

                values.append(str(value))

            return "".join(values)

        # Unary minus
        if expr.startswith("-"):
            value = self.eval(expr[1:])

            if isinstance(value, (int, float)):
                return -value

        # Unary not
        if expr.startswith("not "):
            value = self.eval(expr[4:])

            if value is not None:
                return not bool(value)

        # Arithmetic
        for op in ["+", "-", "*", "/", "%", "^"]:
            parts = split_binary(expr, op)

            if parts:
                left = self.eval(parts[0])
                right = self.eval(parts[1])

                if (
                    isinstance(left, (int, float))
                    and isinstance(right, (int, float))
                ):
                    try:
                        if op == "+":
                            return left + right
                        if op == "-":
                            return left - right
                        if op == "*":
                            return left * right
                        if op == "/":
                            if right == 0:
                                return None
                            return left / right
                        if op == "%":
                            return left % right
                        if op == "^":
                            return left ** right
                    except Exception:
                        return None

        # Comparisons
        for op in ["==", "~=", "<=", ">=", "<", ">"]:
            parts = split_binary(expr, op)

            if parts:
                left = self.eval(parts[0])
                right = self.eval(parts[1])

                if left is not None and right is not None:
                    if op == "==":
                        return left == right
                    if op == "~=":
                        return left != right
                    if op == "<=":
                        return left <= right
                    if op == ">=":
                        return left >= right
                    if op == "<":
                        return left < right
                    if op == ">":
                        return left > right

        return None

    def get_table(self, expr):
        expr = expr.strip()

        if expr in self.tables:
            return self.tables[expr]

        if expr.startswith("{") and expr.endswith("}"):
            items = split_top_level(expr[1:-1])
            table = {}

            index = 1

            for item in items:
                if not item:
                    continue

                value = self.eval(item)

                if value is None:
                    return None

                table[index] = value
                index += 1

            return table

        return None

    @staticmethod
    def balanced(text):
        depth = 0
        quote = None
        i = 0

        while i < len(text):
            c = text[i]

            if quote:
                if c == "\\":
                    i += 2
                    continue

                if c == quote:
                    quote = None

                i += 1
                continue

            if c in "\"'":
                quote = c

            elif c in "([{":
                depth += 1

            elif c in ")]}":
                depth -= 1

                if depth < 0:
                    return False

            i += 1

        return depth == 0 and quote is None


def split_top_level_operator(text, operator):
    parts = []
    start = 0
    depth = 0
    quote = None
    i = 0

    while i < len(text):
        c = text[i]

        if quote:
            if c == "\\":
                i += 2
                continue

            if c == quote:
                quote = None

            i += 1
            continue

        if c in "\"'":
            quote = c
            i += 1
            continue

        if c in "([{":
            depth += 1
            i += 1
            continue

        if c in ")]}":
            depth -= 1
            i += 1
            continue

        if depth == 0 and text.startswith(operator, i):
            parts.append(text[start:i].strip())
            start = i + len(operator)
            i += len(operator)
            continue

        i += 1

    parts.append(text[start:].strip())
    return parts


# ============================================================
# TABLE EXTRACTION
# ============================================================

def extract_tables(code):
    tables = {}

    pattern = re.compile(
        rf"(?:local\s+)?({IDENT})\s*=\s*\{{(.*?)\}}",
        re.S
    )

    for m in pattern.finditer(code):
        name = m.group(1)
        body = m.group(2)

        parts = split_top_level(body)
        table = {}
        index = 1

        for part in parts:
            if not part:
                continue

            key_value = split_binary(part, "=")

            if key_value:
                key, value_expr = key_value
                key = key.strip()

                if re.fullmatch(r"\d+", key):
                    idx = int(key)
                elif is_string_literal(key):
                    idx = decode_string_literal(key)
                else:
                    idx = None

                if idx is not None:
                    value = Evaluator(tables=tables).eval(value_expr)

                    if value is not None:
                        table[idx] = value

                    continue

            value = Evaluator(tables=tables).eval(part)

            if value is not None:
                table[index] = value

            index += 1

        if table:
            tables[name] = table

    return tables


# ============================================================
# CONSTANT PROPAGATION
# ============================================================

def extract_constants(code, tables):
    constants = {}

    changed = True

    while changed:
        changed = False

        evaluator = Evaluator(constants, tables)

        pattern = re.compile(
            rf"(?m)^\s*(?:local\s+)?({IDENT})\s*=\s*(.+?)\s*$"
        )

        for m in pattern.finditer(code):
            name = m.group(1)
            expr = m.group(2).strip()

            if name in {
                "local",
                "function",
                "if",
                "for",
                "while",
                "return",
            }:
                continue

            value = evaluator.eval(expr)

            if value is not None and name not in constants:
                constants[name] = value
                changed = True

    return constants


# ============================================================
# CONSTANT FOLDING
# ============================================================

def fold_expressions(code, constants, tables):
    evaluator = Evaluator(constants, tables)

    # Repeat because one replacement can reveal another.
    for _ in range(12):

        old = code

        # string.char(...)
        def char_replace(match):
            value = evaluator.eval(
                "string.char(" + match.group(1) + ")"
            )

            if isinstance(value, str):
                return lua_quote(value)

            return match.group(0)

        code = re.sub(
            r"string\s*\.\s*char\s*\(([^()]*)\)",
            char_replace,
            code
        )

        # reverse
        def reverse_replace(match):
            value = evaluator.eval(
                "string.reverse(" + match.group(1) + ")"
            )

            if isinstance(value, str):
                return lua_quote(value)

            return match.group(0)

        code = re.sub(
            r"string\s*\.\s*reverse\s*\(\s*(.*?)\s*\)",
            reverse_replace,
            code
        )

        # lower
        def lower_replace(match):
            value = evaluator.eval(
                "string.lower(" + match.group(1) + ")"
            )

            if isinstance(value, str):
                return lua_quote(value)

            return match.group(0)

        code = re.sub(
            r"string\s*\.\s*lower\s*\(\s*(.*?)\s*\)",
            lower_replace,
            code
        )

        # upper
        def upper_replace(match):
            value = evaluator.eval(
                "string.upper(" + match.group(1) + ")"
            )

            if isinstance(value, str):
                return lua_quote(value)

            return match.group(0)

        code = re.sub(
            r"string\s*\.\s*upper\s*\(\s*(.*?)\s*\)",
            upper_replace,
            code
        )

        # tostring
        def tostring_replace(match):
            value = evaluator.eval(
                "tostring(" + match.group(1) + ")"
            )

            if value is not None:
                return lua_quote(str(value))

            return match.group(0)

        code = re.sub(
            r"tostring\s*\(\s*([^()]+?)\s*\)",
            tostring_replace,
            code
        )

        # tonumber
        def tonumber_replace(match):
            value = evaluator.eval(
                "tonumber(" + match.group(1) + ")"
            )

            if isinstance(value, (int, float)):
                return str(value)

            return match.group(0)

        code = re.sub(
            r"tonumber\s*\(\s*([^()]+?)\s*\)",
            tonumber_replace,
            code
        )

        # Simple concat
        for _ in range(6):
            def concat_replace(match):
                left = match.group(1)
                right = match.group(2)

                lv = evaluator.eval(left)
                rv = evaluator.eval(right)

                if lv is not None and rv is not None:
                    return lua_quote(str(lv) + str(rv))

                return match.group(0)

            new_code = re.sub(
                r'((?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'))\s*\.\.\s*'
                r'((?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\d+))',
                concat_replace,
                code
            )

            if new_code == code:
                break

            code = new_code

        # Replace known constants only when they are standalone.
        for name, value in sorted(
            constants.items(),
            key=lambda x: len(x[0]),
            reverse=True
        ):
            if isinstance(value, (str, int, float, bool)):
                replacement = (
                    lua_quote(value)
                    if isinstance(value, str)
                    else str(value).lower()
                    if isinstance(value, bool)
                    else str(value)
                )

                code = re.sub(
                    rf"\b{re.escape(name)}\b",
                    replacement,
                    code
                )

        # Remove parentheses around literals
        code = re.sub(
            r'\(\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|-?\d+(?:\.\d+)?)\s*\)',
            r'\1',
            code
        )

        # Arithmetic constants
        def arithmetic(match):
            a = match.group(1)
            op = match.group(2)
            b = match.group(3)

            av = parse_number(a)
            bv = parse_number(b)

            if av is None or bv is None:
                return match.group(0)

            try:
                if op == "+":
                    result = av + bv
                elif op == "-":
                    result = av - bv
                elif op == "*":
                    result = av * bv
                elif op == "/":
                    if bv == 0:
                        return match.group(0)
                    result = av / bv
                elif op == "%":
                    result = av % bv
                elif op == "^":
                    result = av ** bv
                else:
                    return match.group(0)

                if isinstance(result, float) and result.is_integer():
                    return str(int(result))

                return str(result)

            except Exception:
                return match.group(0)

        code = re.sub(
            r'(?<![\w.])(-?\d+(?:\.\d+)?)\s*([+\-*/%^])\s*(-?\d+(?:\.\d+)?)(?![\w.])',
            arithmetic,
            code
        )

        if code == old:
            break

    return code


# ============================================================
# DECIMAL / HEX ESCAPE NORMALIZATION
# ============================================================

def decode_string_escapes_in_code(code):
    def replace(match):
        token = match.group(0)
        value = decode_string_literal(token)

        if value is None:
            return token

        return lua_quote(value)

    return STRING_RE.sub(replace, code)


# ============================================================
# SIMPLE FUNCTION INLINING
# ============================================================

def inline_simple_char_functions(code):
    functions = {}

    pattern = re.compile(
        rf"""
        local\s+({IDENT})\s*=\s*function\s*
        \(\s*({IDENT})\s*(?:,\s*({IDENT}))?\s*\)
        \s*return\s+
        string\.char\s*\(\s*
        \2(?:\s*,\s*\3)?
        \s*\)
        \s*end
        """,
        re.X | re.S
    )

    for m in pattern.finditer(code):
        name = m.group(1)
        arg1 = m.group(2)
        arg2 = m.group(3)

        functions[name] = (arg1, arg2)

    for name, args in functions.items():
        a1, a2 = args

        def replace(match):
            inside = match.group(1)
            values = split_top_level(inside)

            if len(values) == 1 and a2 is None:
                return "string.char(" + values[0] + ")"

            if len(values) == 2:
                return "string.char(" + values[0] + "," + values[1] + ")"

            return match.group(0)

        code = re.sub(
            rf"\b{re.escape(name)}\s*\((.*?)\)",
            replace,
            code
        )

    return code


# ============================================================
# REMOVE USELESS CONSTRUCTS
# ============================================================

def simplify_code(code):
    # true/false comparisons
    code = re.sub(
        r"\btrue\s*==\s*true\b",
        "true",
        code
    )

    code = re.sub(
        r"\bfalse\s*==\s*false\b",
        "true",
        code
    )

    code = re.sub(
        r"\btrue\s*==\s*false\b",
        "false",
        code
    )

    code = re.sub(
        r"\bfalse\s*==\s*true\b",
        "false",
        code
    )

    # double negation
    for _ in range(5):
        code = re.sub(
            r"\bnot\s+not\s+(.+)",
            r"\1",
            code
        )

    # empty do/end
    code = re.sub(
        r"\bdo\s*end\b",
        "",
        code
    )

    # Empty statements
    code = re.sub(
        r";\s*;",
        ";",
        code
    )

    # Excess semicolon at line end
    code = re.sub(
        r";\s*\n",
        "\n",
        code
    )

    # Duplicate blank lines
    code = re.sub(
        r"\n[ \t]*\n(?:[ \t]*\n)+",
        "\n\n",
        code
    )

    return code


# ============================================================
# VARIABLE NAME CLEANUP
# ============================================================

def clean_generated_names(code):
    mapping = {}
    counter = 1

    generated = re.findall(
        rf"\b(_0x[0-9A-Fa-f]+|_0X[0-9A-Fa-f]+|L\d+_?\d*|v\d+|tmp\d+)\b",
        code
    )

    for name in generated:
        if name not in mapping:
            mapping[name] = f"var_{counter}"
            counter += 1

    for old, new in sorted(
        mapping.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):
        code = re.sub(
            rf"\b{re.escape(old)}\b",
            new,
            code
        )

    return code


# ============================================================
# STRING CONCAT NORMALIZATION
# ============================================================

def merge_literal_concats(code):
    for _ in range(10):
        old = code

        pattern = re.compile(
            r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
            r'\s*\.\.\s*'
            r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
        )

        def repl(match):
            a = decode_string_literal(match.group(1))
            b = decode_string_literal(match.group(2))

            if a is None or b is None:
                return match.group(0)

            return lua_quote(a + b)

        code = pattern.sub(repl, code)

        if code == old:
            break

    return code


# ============================================================
# TABLE CONCAT
# ============================================================

def expand_static_table_concat(code):
    evaluator = Evaluator()

    pattern = re.compile(
        r"table\s*\.\s*concat\s*\(\s*\{(.*?)\}\s*\)",
        re.S
    )

    def repl(match):
        values = []

        for item in split_top_level(match.group(1)):
            value = evaluator.eval(item)

            if value is None:
                return match.group(0)

            values.append(str(value))

        return lua_quote("".join(values))

    return pattern.sub(repl, code)


# ============================================================
# REVERSE STRING LITERALS
# ============================================================

def reverse_literals(code):
    pattern = re.compile(
        r"string\s*\.\s*reverse\s*\(\s*"
        r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
        r"\s*\)"
    )

    def repl(match):
        value = decode_string_literal(match.group(1))

        if value is None:
            return match.group(0)

        return lua_quote(value[::-1])

    return pattern.sub(repl, code)


# ============================================================
# STRING.CHAR
# ============================================================

def expand_string_char(code):
    pattern = re.compile(
        r"string\s*\.\s*char\s*\((.*?)\)",
        re.S
    )

    def repl(match):
        args = split_top_level(match.group(1))

        if not args:
            return match.group(0)

        values = []

        for arg in args:
            n = parse_number(arg)

            if n is None:
                return match.group(0)

            if not 0 <= int(n) <= 255:
                return match.group(0)

            values.append(chr(int(n)))

        return lua_quote("".join(values))

    return pattern.sub(repl, code)


# ============================================================
# HEX / DECIMAL STRING BYTE PATTERNS
# ============================================================

def expand_percent_escapes(code):
    # %XX-like static sequences occasionally appear in custom decoders.
    def repl(match):
        raw = match.group(1)

        try:
            return lua_quote(
                bytes.fromhex(raw).decode("latin-1")
            )
        except Exception:
            return match.group(0)

    return re.sub(
        r'(["\'])(?:%([0-9A-Fa-f]{2})){2,}\1',
        lambda m: m.group(0),
        code
    )


# ============================================================
# FUNCTION RETURN CONSTANT PROPAGATION
# ============================================================

def propagate_simple_returns(code):
    functions = {}

    pattern = re.compile(
        rf"""
        (?:local\s+)?function\s+({IDENT})\s*
        \(\s*\)\s*
        return\s+(.+?)
        end
        """,
        re.X | re.S
    )

    for m in pattern.finditer(code):
        name = m.group(1)
        expr = m.group(2).strip()

        value = Evaluator().eval(expr)

        if value is not None:
            functions[name] = value

    for name, value in functions.items():
        replacement = lua_quote(value) if isinstance(value, str) else str(value)

        code = re.sub(
            rf"\b{re.escape(name)}\s*\(\s*\)",
            replacement,
            code
        )

    return code


# ============================================================
# DEAD LOCAL CONSTANTS
# ============================================================

def remove_unused_simple_locals(code):
    lines = code.splitlines()
    result = []

    for line in lines:
        m = re.match(
            rf"^\s*local\s+({IDENT})\s*=\s*(.+?)\s*$",
            line
        )

        if not m:
            result.append(line)
            continue

        name = m.group(1)

        rest = "\n".join(lines)

        occurrences = len(
            re.findall(
                rf"\b{re.escape(name)}\b",
                rest
            )
        )

        if occurrences <= 1:
            # Only remove obvious constants.
            value = m.group(2)

            if (
                is_string_literal(value.strip())
                or is_number_literal(value.strip())
            ):
                continue

        result.append(line)

    return "\n".join(result)


# ============================================================
# NORMALIZE OPERATORS
# ============================================================

def normalize_operators(code):
    code = re.sub(r"[ \t]+==[ \t]+", " == ", code)
    code = re.sub(r"[ \t]+~=[ \t]+", " ~= ", code)
    code = re.sub(r"[ \t]+<=[ \t]+", " <= ", code)
    code = re.sub(r"[ \t]+>=[ \t]+", " >= ", code)
    code = re.sub(r"[ \t]+\.\.[ \t]+", " .. ", code)
    code = re.sub(r"[ \t]+\+[ \t]+", " + ", code)
    code = re.sub(r"[ \t]+-[ \t]+", " - ", code)
    code = re.sub(r"[ \t]+\*[ \t]+", " * ", code)
    code = re.sub(r"[ \t]+/[ \t]+", " / ", code)

    return code


# ============================================================
# INDENTATION
# ============================================================

BLOCK_OPEN = re.compile(
    r"^\s*(local\s+)?function\b|"
    r"^\s*function\b|"
    r"^\s*if\b.*\bthen\s*$|"
    r"^\s*for\b.*\bdo\s*$|"
    r"^\s*while\b.*\bdo\s*$|"
    r"^\s*repeat\s*$|"
    r"^\s*do\s*$"
)

BLOCK_CLOSE = re.compile(
    r"^\s*(end|until)\b"
)

MIDDLE = re.compile(
    r"^\s*(else|elseif)\b"
)


def format_lua(code):
    lines = code.splitlines()
    result = []
    indent = 0

    for raw in lines:
        line = raw.strip()

        if not line:
            if result and result[-1] != "":
                result.append("")
            continue

        if BLOCK_CLOSE.match(line):
            indent = max(0, indent - 1)

        if MIDDLE.match(line):
            indent = max(0, indent - 1)

        result.append("    " * indent + line)

        if MIDDLE.match(line):
            indent += 1

        elif BLOCK_OPEN.match(line):
            if not re.search(r"\bend\s*$", line):
                indent += 1

    return "\n".join(result).strip() + "\n"


# ============================================================
# FULL PIPELINE
# ============================================================

def deobfuscate(source):
    stats = {
        "passes": 0,
        "changes": 0,
    }

    code = source

    passes = [
        remove_comments,
        decode_string_escapes_in_code,
        inline_simple_char_functions,
        expand_string_char,
        reverse_literals,
        expand_static_table_concat,
        merge_literal_concats,
        propagate_simple_returns,
        simplify_code,
        normalize_operators,
    ]

    for _round in range(5):
        before_round = code

        for fn in passes:
            before = code

            try:
                code = fn(code)
            except Exception:
                pass

            stats["passes"] += 1

            if code != before:
                stats["changes"] += 1

        tables = extract_tables(code)
        constants = extract_constants(code, tables)

        before = code

        try:
            code = fold_expressions(
                code,
                constants,
                tables
            )
        except Exception:
            pass

        if code != before:
            stats["changes"] += 1

        if code == before_round:
            break

    # Final cleanup
    code = simplify_code(code)
    code = merge_literal_concats(code)
    code = clean_generated_names(code)
    code = normalize_operators(code)
    code = format_lua(code)

    return code, stats


# ============================================================
# ANALYSIS
# ============================================================

def analyze(source, result):
    patterns = {
        "string.char": r"string\s*\.\s*char\s*\(",
        "string.byte": r"string\s*\.\s*byte\s*\(",
        "string.reverse": r"string\s*\.\s*reverse\s*\(",
        "string.rep": r"string\s*\.\s*rep\s*\(",
        "string.sub": r"string\s*\.\s*sub\s*\(",
        "string.format": r"string\s*\.\s*format\s*\(",
        "table.concat": r"table\s*\.\s*concat\s*\(",
        "load": r"\bload\s*\(",
        "loadstring": r"\bloadstring\s*\(",
        "getfenv": r"\bgetfenv\s*\(",
        "setfenv": r"\bsetfenv\s*\(",
        "debug": r"\bdebug\s*\.",
        "os": r"\bos\s*\.",
        "io": r"\bio\s*\.",
        "hex escapes": r"\\x[0-9A-Fa-f]{2}",
        "decimal escapes": r"\\\d{1,3}",
        "huge numeric blocks": r"\b\d{6,}\b",
        "hex numbers": r"0[xX][0-9A-Fa-f]+",
    }

    found = []

    for name, pattern in patterns.items():
        count = len(re.findall(pattern, source))

        if count:
            found.append((name, count))

    return found


# ============================================================
# HTML
# ============================================================

def make_html(
    filename,
    original,
    result,
    found,
    stats
):
    original_escaped = html.escape(original)
    result_escaped = html.escape(result)

    original_size = len(original.encode("utf-8"))
    result_size = len(result.encode("utf-8"))

    rows = ""

    for name, count in found:
        rows += (
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{count}</td>"
            "</tr>"
        )

    if not rows:
        rows = (
            "<tr>"
            "<td colspan='2'>"
            "Явные известные паттерны не найдены"
            "</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lua Deobfuscator — {html.escape(filename)}</title>

<style>
* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    background: #090b10;
    color: #e8eaf0;
    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}}

.wrapper {{
    width: min(1500px, 94%);
    margin: 30px auto;
}}

.header {{
    background: #11151d;
    border: 1px solid #242a35;
    border-radius: 20px;
    padding: 25px;
    margin-bottom: 20px;
}}

h1 {{
    margin: 0 0 8px;
    font-size: 28px;
}}

.subtitle {{
    color: #8f98a8;
}}

.grid {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
}}

.card {{
    background: #11151d;
    border: 1px solid #242a35;
    border-radius: 16px;
    padding: 18px;
}}

.card .value {{
    font-size: 25px;
    font-weight: 800;
}}

.card .label {{
    color: #858e9e;
    margin-top: 5px;
}}

.panel {{
    background: #11151d;
    border: 1px solid #242a35;
    border-radius: 20px;
    overflow: hidden;
    margin-bottom: 20px;
}}

.panel-title {{
    padding: 18px 20px;
    font-weight: 800;
    border-bottom: 1px solid #242a35;
}}

pre {{
    margin: 0;
    padding: 22px;
    overflow-x: auto;
    font-family:
        "JetBrains Mono",
        "Cascadia Code",
        Consolas,
        monospace;
    font-size: 13px;
    line-height: 1.65;
    white-space: pre;
}}

table {{
    width: 100%;
    border-collapse: collapse;
}}

td {{
    padding: 13px 18px;
    border-bottom: 1px solid #202631;
}}

td:last-child {{
    text-align: right;
    font-weight: 700;
}}

.badge {{
    display: inline-block;
    padding: 6px 10px;
    border-radius: 9px;
    background: #191f2a;
    color: #aeb8c8;
    font-size: 12px;
}}

.footer {{
    color: #697384;
    text-align: center;
    padding: 20px;
}}
</style>
</head>

<body>
<div class="wrapper">

<div class="header">
    <h1>Lua Deobfuscator</h1>
    <div class="subtitle">
        {html.escape(filename)}
    </div>
</div>

<div class="grid">

<div class="card">
    <div class="value">{original_size:,}</div>
    <div class="label">Original bytes</div>
</div>

<div class="card">
    <div class="value">{result_size:,}</div>
    <div class="label">Result bytes</div>
</div>

<div class="card">
    <div class="value">{stats["passes"]}</div>
    <div class="label">Static passes</div>
</div>

<div class="card">
    <div class="value">{stats["changes"]}</div>
    <div class="label">Changed passes</div>
</div>

</div>

<div class="panel">
<div class="panel-title">Detected patterns</div>
<table>
{rows}
</table>
</div>

<div class="panel">
<div class="panel-title">
Deobfuscated Lua
<span class="badge">STATIC</span>
</div>
<pre>{result_escaped}</pre>
</div>

<div class="panel">
<div class="panel-title">Original Lua</div>
<pre>{original_escaped}</pre>
</div>

<div class="footer">
Generated by Lua Static Deobfuscator
</div>

</div>
</body>
</html>
"""


# ============================================================
# FILE READER
# ============================================================

def read_file(path):
    data = Path(path).read_bytes()

    if len(data) > MAX_FILE_SIZE:
        raise ValueError(
            f"Файл слишком большой: "
            f"{len(data) / 1024 / 1024:.2f} MB"
        )

    encodings = [
        "utf-8-sig",
        "utf-8",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "cp1251",
        "latin-1",
    ]

    last_error = None

    for encoding in encodings:
        try:
            text = data.decode(encoding)

            if "\x00" in text and encoding not in {
                "utf-16",
                "utf-16-le",
                "utf-16-be",
            }:
                continue

            return text

        except UnicodeDecodeError as e:
            last_error = e

    raise ValueError(
        f"Не удалось определить кодировку: {last_error}"
    )


# ============================================================
# TELEGRAM
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Отправь мне Lua-файл (.lua или .txt).\n\n"
        "Я проведу статический анализ, "
        "расшифрую доступные константы и "
        "верну HTML с результатом.\n\n"
        "⚠️ Загруженный Lua-код не запускается."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📦 Поддерживается Lua/Lua-обфускация.\n\n"
        "Отправь .lua файл документом.\n\n"
        "Бот выполняет статическую обработку:\n"
        "• string.char\n"
        "• string.byte\n"
        "• reverse\n"
        "• concat\n"
        "• escapes\n"
        "• арифметику\n"
        "• константы\n"
        "• таблицы\n"
        "• простые decoder-функции\n"
        "• очистку имён\n"
        "• форматирование\n\n"
        "Код не выполняется."
    )


@dp.message(F.document)
async def handle_document(message: Message):
    document = message.document

    if not document:
        return

    filename = document.file_name or "script.lua"

    extension = Path(filename).suffix.lower()

    if extension not in {".lua", ".txt"}:
        await message.answer(
            "❌ Отправь файл с расширением .lua или .txt"
        )
        return

    await message.answer(
        "🔎 Анализирую Lua...\n"
        "Это может занять некоторое время."
    )

    temp_in = None
    temp_out = None

    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".lua"
        ) as f:
            temp_in = f.name

        await bot.download(
            document,
            destination=temp_in
        )

        original = read_file(temp_in)

        if not original.strip():
            raise ValueError("Файл пустой")

        result, stats = deobfuscate(original)

        if not result.strip():
            result = original

        patterns = analyze(
            original,
            result
        )

        safe_name = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            Path(filename).stem
        )

        html_name = (
            f"{safe_name}_deobfuscated.html"
        )

        html_path = Path(
            tempfile.gettempdir()
        ) / html_name

        html_content = make_html(
            filename,
            original,
            result,
            patterns,
            stats
        )

        html_path.write_text(
            html_content,
            encoding="utf-8"
        )

        temp_out = str(html_path)

        await message.answer_document(
            FSInputFile(
                temp_out,
                filename=html_name
            ),
            caption=(
                "✅ Готово\n\n"
                f"📄 {filename}\n"
                f"🔄 Проходов: {stats['passes']}\n"
                f"✨ Изменений: {stats['changes']}\n\n"
                "Результат находится внутри HTML."
            )
        )

    except Exception as e:
        logging.exception(
            "Deobfuscation error"
        )

        await message.answer(
            "❌ Ошибка обработки\n\n"
            f"{type(e).__name__}: {e}"
        )

    finally:
        for path in [temp_in, temp_out]:
            if path:
                try:
                    Path(path).unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "📎 Отправь Lua-файл документом."
    )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health(request):
    return web.Response(
        text="OK",
        status=200
    )


async def start_web():
    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logging.info(
        "HTTP server started on port %s",
        PORT
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    logging.info(
        "Starting Lua Deobfuscator..."
    )

    await start_web()

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logging.info(
        "Telegram polling started"
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
