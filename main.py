import os
import re
import ast
import html
import asyncio
import logging
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional

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
    raise RuntimeError("BOT_TOKEN is missing")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# VALUES
# ============================================================

@dataclass
class Unknown:
    reason: str = ""

    def __bool__(self):
        return False


UNKNOWN = Unknown()


@dataclass
class LuaFunction:
    params: list
    body: str
    return_expr: Optional[str] = None


@dataclass
class LuaTable:
    items: dict

    def get(self, key, default=UNKNOWN):
        return self.items.get(key, default)

    def set(self, key, value):
        self.items[key] = value

    def length(self):
        n = 0

        while (n + 1) in self.items:
            n += 1

        return n


# ============================================================
# BASIC VALUE HELPERS
# ============================================================

def is_unknown(v):
    return isinstance(v, Unknown)


def lua_bool(v):
    if v is None:
        return False

    if v is False:
        return False

    return True


def lua_tostring(v):
    if isinstance(v, str):
        return v

    if v is True:
        return "true"

    if v is False:
        return "false"

    if v is None:
        return "nil"

    if isinstance(v, float) and v.is_integer():
        return str(int(v))

    if isinstance(v, (int, float)):
        return str(v)

    return str(v)


def lua_quote(value):
    if not isinstance(value, str):
        value = lua_tostring(value)

    value = (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\0", "\\0")
    )

    return '"' + value + '"'


# ============================================================
# STRING DECODER
# ============================================================

def decode_lua_string(token):
    token = token.strip()

    if len(token) < 2:
        return UNKNOWN

    if token[0] not in "\"'":
        return UNKNOWN

    if token[-1] != token[0]:
        return UNKNOWN

    raw = token[1:-1]

    out = []
    i = 0

    escapes = {
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

    while i < len(raw):
        if raw[i] != "\\":
            out.append(raw[i])
            i += 1
            continue

        if i + 1 >= len(raw):
            out.append("\\")
            break

        c = raw[i + 1]

        if c in escapes:
            out.append(escapes[c])
            i += 2
            continue

        m = re.match(r"[0-9]{1,3}", raw[i + 1:])

        if m:
            try:
                out.append(chr(int(m.group(0)) & 255))
                i += 1 + len(m.group(0))
                continue
            except Exception:
                pass

        if c == "x" and i + 3 < len(raw):
            h = raw[i + 2:i + 4]

            try:
                out.append(chr(int(h, 16)))
                i += 4
                continue
            except Exception:
                pass

        if c == "u" and i + 2 < len(raw) and raw[i + 2] == "{":
            end = raw.find("}", i + 3)

            if end != -1:
                try:
                    out.append(
                        chr(int(raw[i + 3:end], 16))
                    )
                    i = end + 1
                    continue
                except Exception:
                    pass

        out.append(c)
        i += 2

    return "".join(out)


# ============================================================
# TOKENIZER
# ============================================================

@dataclass
class Token:
    value: str
    kind: str


TOKEN_RE = re.compile(
    r"""
    (?P<WS>\s+)
    |
    (?P<COMMENT>--[^\n]*)
    |
    (?P<LONGCOMMENT>--\[(=*)\[.*?\]\1\])
    |
    (?P<STRING>
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
    )
    |
    (?P<NUMBER>
        0[xX][0-9A-Fa-f]+
        |
        0[bB][01]+
        |
        0[oO][0-7]+
        |
        \d+(?:\.\d+)?
    )
    |
    (?P<ID>[A-Za-z_][A-Za-z0-9_]*)
    |
    (?P<OP>
        \.\.
        |
        ==|~=|<=|>=
        |
        \+=|-=|\*=|/=
        |
        \.\.\.
        |
        [+\-*/%^#=<>.,:{}()\[\];]
    )
    """,
    re.X | re.S
)


def tokenize(code):
    tokens = []

    for m in TOKEN_RE.finditer(code):
        kind = m.lastgroup
        value = m.group(0)

        if kind in {"WS", "COMMENT", "LONGCOMMENT"}:
            continue

        tokens.append(
            Token(value, kind)
        )

    return tokens


# ============================================================
# TOP LEVEL SCANNER
# ============================================================

def split_top_level(text, delimiter=","):
    result = []
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

        elif c == delimiter and depth == 0:
            result.append(
                text[start:i].strip()
            )
            start = i + 1

        i += 1

    tail = text[start:].strip()

    if tail:
        result.append(tail)

    return result


def find_matching(text, start, opening="(", closing=")"):
    depth = 0
    quote = None
    i = start

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

        if c == opening:
            depth += 1

        elif c == closing:
            depth -= 1

            if depth == 0:
                return i

        i += 1

    return -1


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
            return (
                text[:i].strip(),
                text[i + len(operator):].strip()
            )

        i += 1

    return None


# ============================================================
# STATIC LUA EVALUATOR
# ============================================================

class StaticLua:
    def __init__(self):
        self.env = {}
        self.functions = {}
        self.tables = {}
        self.output_aliases = {}
        self.depth = 0

    # --------------------------------------------------------
    # MAIN EXPRESSION EVALUATOR
    # --------------------------------------------------------

    def eval(self, expr, local_env=None):
        if self.depth > 80:
            return UNKNOWN

        expr = expr.strip()

        if not expr:
            return UNKNOWN

        if local_env is None:
            local_env = self.env

        self.depth += 1

        try:
            return self._eval(expr, local_env)
        finally:
            self.depth -= 1

    def _eval(self, expr, env):
        # ----------------------------------------------------
        # Outer parentheses
        # ----------------------------------------------------

        while (
            expr.startswith("(")
            and expr.endswith(")")
            and find_matching(expr, 0) == len(expr) - 1
        ):
            expr = expr[1:-1].strip()

        # ----------------------------------------------------
        # Strings
        # ----------------------------------------------------

        if (
            len(expr) >= 2
            and expr[0] in "\"'"
            and expr[-1] == expr[0]
        ):
            return decode_lua_string(expr)

        # ----------------------------------------------------
        # Numbers
        # ----------------------------------------------------

        n = self.parse_number(expr)

        if n is not None:
            return n

        # ----------------------------------------------------
        # Booleans / nil
        # ----------------------------------------------------

        if expr == "true":
            return True

        if expr == "false":
            return False

        if expr == "nil":
            return None

        # ----------------------------------------------------
        # Variable
        # ----------------------------------------------------

        if re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            expr
        ):
            if expr in env:
                return env[expr]

            if expr in self.env:
                return self.env[expr]

            return UNKNOWN

        # ----------------------------------------------------
        # Table literal
        # ----------------------------------------------------

        if expr.startswith("{") and expr.endswith("}"):
            return self.eval_table(
                expr[1:-1],
                env
            )

        # ----------------------------------------------------
        # Length
        # ----------------------------------------------------

        if expr.startswith("#"):
            value = self.eval(
                expr[1:].strip(),
                env
            )

            if isinstance(value, LuaTable):
                return value.length()

            if isinstance(value, str):
                return len(value)

            return UNKNOWN

        # ----------------------------------------------------
        # Unary not
        # ----------------------------------------------------

        if expr.startswith("not "):
            value = self.eval(
                expr[4:],
                env
            )

            if is_unknown(value):
                return UNKNOWN

            return not lua_bool(value)

        # ----------------------------------------------------
        # Unary minus
        # ----------------------------------------------------

        if expr.startswith("-"):
            value = self.eval(
                expr[1:],
                env
            )

            if isinstance(value, (int, float)):
                return -value

        # ----------------------------------------------------
        # Function calls / method calls
        # ----------------------------------------------------

        call = self.parse_call(expr)

        if call:
            name, args, method = call

            return self.call_function(
                name,
                args,
                env,
                method
            )

        # ----------------------------------------------------
        # Index access
        # ----------------------------------------------------

        access = self.parse_index(expr)

        if access:
            base_expr, key_expr = access

            base = self.eval(
                base_expr,
                env
            )

            key = self.eval(
                key_expr,
                env
            )

            return self.index_value(
                base,
                key
            )

        # ----------------------------------------------------
        # Concatenation
        # ----------------------------------------------------

        parts = self.split_operator(
            expr,
            ".."
        )

        if len(parts) > 1:
            values = []

            for part in parts:
                value = self.eval(
                    part,
                    env
                )

                if is_unknown(value):
                    return UNKNOWN

                values.append(
                    lua_tostring(value)
                )

            return "".join(values)

        # ----------------------------------------------------
        # OR
        # ----------------------------------------------------

        parts = self.split_operator(
            expr,
            " or "
        )

        if len(parts) > 1:
            for part in parts:
                value = self.eval(
                    part,
                    env
                )

                if is_unknown(value):
                    continue

                if lua_bool(value):
                    return value

            return UNKNOWN

        # ----------------------------------------------------
        # AND
        # ----------------------------------------------------

        parts = self.split_operator(
            expr,
            " and "
        )

        if len(parts) > 1:
            result = None

            for part in parts:
                value = self.eval(
                    part,
                    env
                )

                if is_unknown(value):
                    return UNKNOWN

                if not lua_bool(value):
                    return value

                result = value

            return result

        # ----------------------------------------------------
        # Comparisons
        # ----------------------------------------------------

        for op in [
            "==",
            "~=",
            "<=",
            ">=",
            "<",
            ">"
        ]:
            parts = self.split_operator(
                expr,
                op
            )

            if len(parts) == 2:
                a = self.eval(parts[0], env)
                b = self.eval(parts[1], env)

                if is_unknown(a) or is_unknown(b):
                    return UNKNOWN

                try:
                    if op == "==":
                        return a == b
                    if op == "~=":
                        return a != b
                    if op == "<=":
                        return a <= b
                    if op == ">=":
                        return a >= b
                    if op == "<":
                        return a < b
                    if op == ">":
                        return a > b
                except Exception:
                    return UNKNOWN

        # ----------------------------------------------------
        # Arithmetic
        # ----------------------------------------------------

        for op in [
            "+",
            "-",
            "*",
            "/",
            "%",
            "^"
        ]:
            parts = self.split_operator(
                expr,
                op
            )

            if len(parts) == 2:
                a = self.eval(parts[0], env)
                b = self.eval(parts[1], env)

                if not isinstance(a, (int, float)):
                    return UNKNOWN

                if not isinstance(b, (int, float)):
                    return UNKNOWN

                try:
                    if op == "+":
                        return a + b

                    if op == "-":
                        return a - b

                    if op == "*":
                        return a * b

                    if op == "/":
                        if b == 0:
                            return UNKNOWN
                        return a / b

                    if op == "%":
                        return a % b

                    if op == "^":
                        return a ** b

                except Exception:
                    return UNKNOWN

        return UNKNOWN

    # --------------------------------------------------------
    # NUMBER
    # --------------------------------------------------------

    def parse_number(self, text):
        text = text.strip()

        try:
            if re.fullmatch(
                r"0[xX][0-9A-Fa-f]+",
                text
            ):
                return int(text, 16)

            if re.fullmatch(
                r"0[bB][01]+",
                text
            ):
                return int(text, 2)

            if re.fullmatch(
                r"0[oO][0-7]+",
                text
            ):
                return int(text, 8)

            if re.fullmatch(
                r"-?\d+",
                text
            ):
                return int(text)

            if re.fullmatch(
                r"-?\d+\.\d+",
                text
            ):
                return float(text)

        except Exception:
            pass

        return None

    # --------------------------------------------------------
    # OPERATOR SPLIT
    # --------------------------------------------------------

    def split_operator(self, text, operator):
        result = []
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

            if depth == 0 and text.startswith(
                operator,
                i
            ):
                result.append(
                    text[start:i].strip()
                )

                start = i + len(operator)
                i += len(operator)
                continue

            i += 1

        result.append(
            text[start:].strip()
        )

        return result

    # --------------------------------------------------------
    # TABLE
    # --------------------------------------------------------

    def eval_table(self, body, env):
        table = LuaTable({})
        index = 1

        for item in split_top_level(body):
            if not item:
                continue

            # [key] = value
            m = re.match(
                r"^\s*\[(.*?)\]\s*=\s*(.*)$",
                item,
                re.S
            )

            if m:
                key = self.eval(
                    m.group(1),
                    env
                )

                value = self.eval(
                    m.group(2),
                    env
                )

                if is_unknown(key) or is_unknown(value):
                    continue

                table.set(
                    key,
                    value
                )

                continue

            # name = value
            m = re.match(
                r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$",
                item,
                re.S
            )

            if m:
                key = m.group(1)

                value = self.eval(
                    m.group(2),
                    env
                )

                if not is_unknown(value):
                    table.set(
                        key,
                        value
                    )

                continue

            # Array item
            value = self.eval(
                item,
                env
            )

            if not is_unknown(value):
                table.set(
                    index,
                    value
                )

            index += 1

        return table

    # --------------------------------------------------------
    # INDEX
    # --------------------------------------------------------

    def parse_index(self, expr):
        depth = 0
        quote = None

        for i in range(len(expr) - 1, -1, -1):
            c = expr[i]

            if quote:
                if c == quote:
                    quote = None

                continue

            if c in "\"'":
                quote = c
                continue

            if c == "]":
                depth += 1

            elif c == "[":
                depth -= 1

                if depth == 0:
                    if i == 0:
                        return None

                    if not expr.endswith("]"):
                        return None

                    return (
                        expr[:i].strip(),
                        expr[i + 1:-1].strip()
                    )

        # .field
        m = re.match(
            r"^(.*?)\.([A-Za-z_][A-Za-z0-9_]*)$",
            expr,
            re.S
        )

        if m:
            return (
                m.group(1).strip(),
                '"' + m.group(2) + '"'
            )

        return None

    # --------------------------------------------------------
    # INDEX VALUE
    # --------------------------------------------------------

    def index_value(self, base, key):
        if isinstance(base, LuaTable):
            return base.get(key)

        if isinstance(base, str):
            if isinstance(key, (int, float)):
                index = int(key)

                if 1 <= index <= len(base):
                    return base[index - 1]

        return UNKNOWN

    # --------------------------------------------------------
    # CALL PARSER
    # --------------------------------------------------------

    def parse_call(self, expr):
        expr = expr.strip()

        # name(args)
        m = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(",
            expr
        )

        if m:
            name = m.group(1)

            pos = m.end() - 1
            end = find_matching(
                expr,
                pos
            )

            if end == len(expr) - 1:
                inside = expr[
                    pos + 1:end
                ]

                return (
                    name,
                    split_top_level(inside),
                    None
                )

        # obj:method(args)
        m = re.match(
            r"^(.+?):([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            expr,
            re.S
        )

        if m:
            object_expr = m.group(1).strip()
            method = m.group(2)

            pos = m.end() - 1

            end = find_matching(
                expr,
                pos
            )

            if end == len(expr) - 1:
                inside = expr[
                    pos + 1:end
                ]

                return (
                    object_expr,
                    split_top_level(inside),
                    method
                )

        return None

    # --------------------------------------------------------
    # CALL FUNCTION
    # --------------------------------------------------------

    def call_function(
        self,
        name,
        arg_exprs,
        env,
        method=None
    ):
        # ----------------------------------------------------
        # String methods
        # ----------------------------------------------------

        if method:
            obj = self.eval(
                name,
                env
            )

            args = [
                self.eval(x, env)
                for x in arg_exprs
            ]

            if is_unknown(obj):
                return UNKNOWN

            if method == "reverse":
                if isinstance(obj, str):
                    return obj[::-1]

            if method == "upper":
                if isinstance(obj, str):
                    return obj.upper()

            if method == "lower":
                if isinstance(obj, str):
                    return obj.lower()

            if method == "sub":
                if isinstance(obj, str):
                    if not args:
                        return UNKNOWN

                    start = args[0]

                    if not isinstance(start, (int, float)):
                        return UNKNOWN

                    start = int(start)

                    if start < 0:
                        start = len(obj) + start + 1

                    start = max(1, start)

                    if len(args) >= 2:
                        stop = args[1]

                        if not isinstance(
                            stop,
                            (int, float)
                        ):
                            return UNKNOWN

                        stop = int(stop)

                        if stop < 0:
                            stop = len(obj) + stop + 1

                        return obj[
                            start - 1:stop
                        ]

                    return obj[start - 1:]

            return UNKNOWN

        # ----------------------------------------------------
        # Builtins
        # ----------------------------------------------------

        args = [
            self.eval(x, env)
            for x in arg_exprs
        ]

        if name in {
            "print",
            "warn",
            "error"
        }:
            return UNKNOWN

        if name == "string.char":
            if any(is_unknown(x) for x in args):
                return UNKNOWN

            try:
                return "".join(
                    chr(int(x) & 255)
                    for x in args
                )
            except Exception:
                return UNKNOWN

        if name == "string.byte":
            if not args:
                return UNKNOWN

            if not isinstance(args[0], str):
                return UNKNOWN

            pos = 1

            if len(args) >= 2:
                if isinstance(
                    args[1],
                    (int, float)
                ):
                    pos = int(args[1])

            if 1 <= pos <= len(args[0]):
                return ord(args[0][pos - 1])

            return UNKNOWN

        if name == "string.reverse":
            if len(args) == 1 and isinstance(
                args[0],
                str
            ):
                return args[0][::-1]

            return UNKNOWN

        if name == "string.lower":
            if len(args) == 1 and isinstance(
                args[0],
                str
            ):
                return args[0].lower()

            return UNKNOWN

        if name == "string.upper":
            if len(args) == 1 and isinstance(
                args[0],
                str
            ):
                return args[0].upper()

            return UNKNOWN

        if name == "string.len":
            if len(args) == 1 and isinstance(
                args[0],
                str
            ):
                return len(args[0])

            return UNKNOWN

        if name == "string.rep":
            if len(args) >= 2:
                if (
                    isinstance(args[0], str)
                    and isinstance(args[1], (int, float))
                ):
                    return args[0] * int(args[1])

            return UNKNOWN

        if name == "string.sub":
            if len(args) < 2:
                return UNKNOWN

            s = args[0]

            if not isinstance(s, str):
                return UNKNOWN

            start = args[1]

            if not isinstance(start, (int, float)):
                return UNKNOWN

            start = int(start)

            if start < 0:
                start = len(s) + start + 1

            start = max(1, start)

            if len(args) >= 3:
                stop = args[2]

                if not isinstance(
                    stop,
                    (int, float)
                ):
                    return UNKNOWN

                stop = int(stop)

                if stop < 0:
                    stop = len(s) + stop + 1

                return s[start - 1:stop]

            return s[start - 1:]

        if name == "table.concat":
            if not args:
                return UNKNOWN

            table = args[0]

            if not isinstance(
                table,
                LuaTable
            ):
                return UNKNOWN

            separator = ""

            if len(args) >= 2:
                if isinstance(args[1], str):
                    separator = args[1]

            values = []

            for i in range(
                1,
                table.length() + 1
            ):
                value = table.get(i)

                if is_unknown(value):
                    return UNKNOWN

                values.append(
                    lua_tostring(value)
                )

            return separator.join(values)

        if name == "table.insert":
            if len(args) == 2:
                table = args[0]

                if isinstance(table, LuaTable):
                    table.set(
                        table.length() + 1,
                        args[1]
                    )

                    return None

            if len(args) == 3:
                table = args[0]

                if isinstance(table, LuaTable):
                    pos = args[1]

                    if isinstance(
                        pos,
                        (int, float)
                    ):
                        pos = int(pos)

                        for i in range(
                            table.length(),
                            pos - 1,
                            -1
                        ):
                            table.set(
                                i + 1,
                                table.get(i)
                            )

                        table.set(
                            pos,
                            args[2]
                        )

                        return None

            return UNKNOWN

        if name == "tonumber":
            if not args:
                return UNKNOWN

            value = args[0]

            if isinstance(
                value,
                (int, float)
            ):
                return value

            if isinstance(value, str):
                try:
                    return int(value)
                except Exception:
                    try:
                        return float(value)
                    except Exception:
                        return UNKNOWN

        if name == "tostring":
            if not args:
                return UNKNOWN

            return lua_tostring(args[0])

        if name == "string.format":
            if not args:
                return UNKNOWN

            fmt = args[0]

            if not isinstance(fmt, str):
                return UNKNOWN

            values = args[1:]

            # Important for generated load()
            if fmt == "%q" and values:
                return lua_quote(
                    lua_tostring(values[0])
                )

            try:
                result = fmt

                for value in values:
                    if "%q" in result:
                        result = result.replace(
                            "%q",
                            lua_quote(
                                lua_tostring(value)
                            ),
                            1
                        )

                    elif "%s" in result:
                        result = result.replace(
                            "%s",
                            lua_tostring(value),
                            1
                        )

                    elif "%d" in result:
                        result = result.replace(
                            "%d",
                            str(int(value)),
                            1
                        )

                return result

            except Exception:
                return UNKNOWN

        # ----------------------------------------------------
        # User-defined function
        # ----------------------------------------------------

        if name in self.functions:
            fn = self.functions[name]

            if not isinstance(
                fn,
                LuaFunction
            ):
                return UNKNOWN

            child = dict(self.env)

            for i, param in enumerate(
                fn.params
            ):
                if i < len(args):
                    child[param] = args[i]
                else:
                    child[param] = None

            # If it is a pure return expression
            if fn.return_expr:
                return self.eval(
                    fn.return_expr,
                    child
                )

            # Full tiny body interpreter
            return self.execute_body(
                fn.body,
                child
            )

        # ----------------------------------------------------
        # Alias
        # ----------------------------------------------------

        if name in self.env:
            fn = self.env[name]

            if isinstance(
                fn,
                LuaFunction
            ):
                child = dict(self.env)

                for i, param in enumerate(
                    fn.params
                ):
                    if i < len(args):
                        child[param] = args[i]

                if fn.return_expr:
                    return self.eval(
                        fn.return_expr,
                        child
                    )

        return UNKNOWN

    # --------------------------------------------------------
    # FUNCTION COLLECTION
    # --------------------------------------------------------

    def collect_functions(self, code):
        # local name = function(a,b) ... end
        pattern = re.compile(
            r"""
            (?:local\s+)?
            ([A-Za-z_][A-Za-z0-9_]*)
            \s*=\s*function\s*
            \((.*?)\)
            (.*?)
            \bend
            """,
            re.X | re.S
        )

        for m in pattern.finditer(code):
            name = m.group(1)
            params = [
                x.strip()
                for x in m.group(2).split(",")
                if x.strip()
            ]

            body = m.group(3).strip()

            return_expr = None

            rm = re.search(
                r"\breturn\s+(.+?)(?:;|$)",
                body,
                re.S
            )

            if rm:
                return_expr = rm.group(1).strip()

            self.functions[name] = LuaFunction(
                params=params,
                body=body,
                return_expr=return_expr
            )

    # --------------------------------------------------------
    # GLOBAL ASSIGNMENTS
    # --------------------------------------------------------

    def collect_assignments(self, code):
        # Multiple iterations are intentional.
        for _ in range(20):
            changed = False

            pattern = re.compile(
                r"""
                (?m)^\s*
                (?:local\s+)?
                ([A-Za-z_][A-Za-z0-9_]*)
                \s*=\s*(.+?)
                \s*$
                """,
                re.X
            )

            for m in pattern.finditer(code):
                name = m.group(1)
                expr = m.group(2).strip()

                # Don't treat function definitions as values here.
                if expr.startswith("function"):
                    continue

                value = self.eval(
                    expr,
                    self.env
                )

                if not is_unknown(value):
                    if (
                        name not in self.env
                        or self.env[name] != value
                    ):
                        self.env[name] = value
                        changed = True

            if not changed:
                break

    # --------------------------------------------------------
    # BODY EXECUTION
    # --------------------------------------------------------

    def execute_body(self, body, env):
        # ----------------------------------------------------
        # local assignments
        # ----------------------------------------------------

        for m in re.finditer(
            r"(?m)^\s*local\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)"
            r"\s*=\s*(.+?)\s*$",
            body
        ):
            name = m.group(1)
            expr = m.group(2)

            value = self.eval(
                expr,
                env
            )

            if not is_unknown(value):
                env[name] = value

        # ----------------------------------------------------
        # Numeric for
        # ----------------------------------------------------

        for m in re.finditer(
            r"""
            for\s+
            ([A-Za-z_][A-Za-z0-9_]*)\s*=\s*
            (.*?),\s*(.*?)(?:,\s*(.*?))?\s*
            do
            (.*?)
            end
            """,
            body,
            re.X | re.S
        ):
            var = m.group(1)
            start = self.eval(
                m.group(2),
                env
            )
            stop = self.eval(
                m.group(3),
                env
            )

            step = 1

            if m.group(4):
                step = self.eval(
                    m.group(4),
                    env
                )

            inner = m.group(5)

            if not all(
                isinstance(x, (int, float))
                for x in [start, stop, step]
            ):
                continue

            if step == 0:
                continue

            current = int(start)
            stop = int(stop)
            step = int(step)

            count = 0

            while (
                current <= stop
                if step > 0
                else current >= stop
            ):
                if count > 10000:
                    break

                env[var] = current

                # local assignments inside loop
                for lm in re.finditer(
                    r"""
                    local\s+
                    ([A-Za-z_][A-Za-z0-9_]*)\s*=\s*
                    (.+?)
                    (?=\n|$)
                    """,
                    inner,
                    re.X
                ):
                    value = self.eval(
                        lm.group(2),
                        env
                    )

                    if not is_unknown(value):
                        env[lm.group(1)] = value

                # table assignments
                for am in re.finditer(
                    r"""
                    ([A-Za-z_][A-Za-z0-9_]*)\s*
                    \[\s*(.*?)\s*\]\s*=\s*
                    (.+?)
                    (?=\n|$)
                    """,
                    inner,
                    re.X
                ):
                    table_name = am.group(1)
                    key = self.eval(
                        am.group(2),
                        env
                    )
                    value = self.eval(
                        am.group(3),
                        env
                    )

                    table = env.get(
                        table_name
                    )

                    if (
                        isinstance(table, LuaTable)
                        and not is_unknown(key)
                        and not is_unknown(value)
                    ):
                        table.set(
                            key,
                            value
                        )

                current += step
                count += 1

        # ----------------------------------------------------
        # return
        # ----------------------------------------------------

        matches = list(
            re.finditer(
                r"\breturn\s+(.+?)(?:;|\n|$)",
                body,
                re.S
            )
        )

        if matches:
            expr = matches[-1].group(1).strip()

            return self.eval(
                expr,
                env
            )

        return UNKNOWN

    # --------------------------------------------------------
    # LOAD STATIC UNWRAPPER
    # --------------------------------------------------------

    def unwrap_static_load(self, code):
        pattern = re.compile(
            r"""
            \bload\s*\(
            (.*?)
            \)
            """,
            re.X | re.S
        )

        replacements = []

        for m in pattern.finditer(code):
            expr = m.group(1)

            value = self.eval(
                expr,
                self.env
            )

            if isinstance(value, str):
                # We don't execute it.
                # We only expose the generated source.
                replacements.append(
                    (
                        m.start(),
                        m.end(),
                        value
                    )
                )

        for start, end, value in reversed(
            replacements
        ):
            code = (
                code[:start]
                + value
                + code[end:]
            )

        return code


# ============================================================
# AST-LIKE SOURCE CLEANER
# ============================================================

def remove_comments(code):
    code = re.sub(
        r"--\[(=*)\[.*?\]\1\]",
        "",
        code,
        flags=re.S
    )

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

        if (
            c == "-"
            and i + 1 < len(code)
            and code[i + 1] == "-"
        ):
            while (
                i < len(code)
                and code[i] != "\n"
            ):
                i += 1

            result.append("\n")
            continue

        result.append(c)
        i += 1

    return "".join(result)


# ============================================================
# APPLY STATIC VALUES TO SOURCE
# ============================================================

def replace_known_constants(code, env):
    # Longest first.
    for name, value in sorted(
        env.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            name
        ):
            continue

        if isinstance(value, str):
            replacement = lua_quote(value)

        elif isinstance(value, bool):
            replacement = (
                "true"
                if value
                else "false"
            )

        elif isinstance(
            value,
            (int, float)
        ):
            replacement = (
                str(int(value))
                if isinstance(value, float)
                and value.is_integer()
                else str(value)
            )

        elif isinstance(value, LuaTable):
            continue

        else:
            continue

        code = re.sub(
            rf"\b{re.escape(name)}\b",
            replacement,
            code
        )

    return code


# ============================================================
# TABLE TO SOURCE
# ============================================================

def value_to_source(value):
    if isinstance(value, str):
        return lua_quote(value)

    if value is True:
        return "true"

    if value is False:
        return "false"

    if value is None:
        return "nil"

    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))

        return str(value)

    if isinstance(value, LuaTable):
        parts = []

        for key, val in value.items.items():
            if isinstance(key, int):
                parts.append(
                    value_to_source(val)
                )
            else:
                parts.append(
                    "["
                    + value_to_source(key)
                    + "]="
                    + value_to_source(val)
                )

        return "{ " + ", ".join(parts) + " }"

    return None


# ============================================================
# CLEAN GENERATED VARIABLES
# ============================================================

def clean_names(code):
    names = re.findall(
        r"\b_0[xX][0-9A-Fa-f]+\b",
        code
    )

    mapping = {}

    counter = 1

    for name in names:
        if name not in mapping:
            mapping[name] = (
                f"decoded_{counter}"
            )
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
# REMOVE TRIVIAL FUNCTION DEFINITIONS
# ============================================================

def remove_redundant_functions(
    code,
    evaluator
):
    removable = []

    for name, fn in evaluator.functions.items():
        if not fn.return_expr:
            continue

        # Count calls.
        calls = len(
            re.findall(
                rf"\b{re.escape(name)}\s*\(",
                code
            )
        )

        if calls == 0:
            removable.append(name)

    for name in removable:
        pattern = re.compile(
            rf"""
            (?:local\s+)?
            {re.escape(name)}
            \s*=\s*function\s*
            \(
            .*?
            \)
            .*?
            \bend
            """,
            re.X | re.S
        )

        code = pattern.sub(
            "",
            code,
            count=1
        )

    return code


# ============================================================
# COLLAPSE STRING FUNCTIONS
# ============================================================

def collapse_calls(code, evaluator):
    for _ in range(15):
        old = code

        # Function calls whose arguments are simple.
        pattern = re.compile(
            r"""
            \b
            ([A-Za-z_][A-Za-z0-9_]*)
            \s*
            \(
                ([^()]|\([^()]*\))*
            \)
            """,
            re.X
        )

        matches = list(
            pattern.finditer(code)
        )

        for m in reversed(matches):
            whole = m.group(0)

            # Don't replace language constructs.
            if m.group(1) in {
                "if",
                "for",
                "while",
                "function",
                "return",
                "local"
            }:
                continue

            value = evaluator.eval(
                whole,
                evaluator.env
            )

            if is_unknown(value):
                continue

            if isinstance(
                value,
                (str, int, float, bool)
            ):
                code = (
                    code[:m.start()]
                    + value_to_source(value)
                    + code[m.end():]
                )

        if code == old:
            break

    return code


# ============================================================
# STATIC PASS
# ============================================================

def deobfuscate(source):
    stats = {
        "rounds": 0,
        "changes": 0,
        "functions": 0,
        "constants": 0,
    }

    code = source

    # Remove comments first.
    code = remove_comments(code)

    evaluator = StaticLua()

    # --------------------------------------------------------
    # Repeated static analysis
    # --------------------------------------------------------

    for round_no in range(20):
        stats["rounds"] += 1

        before = code

        evaluator.collect_functions(code)

        # Resolve functions repeatedly.
        evaluator.collect_assignments(code)

        # Execute only the tiny static evaluator.
        evaluator.unwrap_static_load(code)

        # Expand load source.
        new_code = evaluator.unwrap_static_load(
            code
        )

        if new_code != code:
            code = new_code
            stats["changes"] += 1

            # Analyze generated Lua again.
            continue

        # Replace function calls.
        new_code = collapse_calls(
            code,
            evaluator
        )

        if new_code != code:
            code = new_code
            stats["changes"] += 1

        # Recalculate globals.
        evaluator.collect_functions(code)
        evaluator.collect_assignments(code)

        # Substitute constants.
        new_code = replace_known_constants(
            code,
            evaluator.env
        )

        # Do NOT replace "print" or keywords.
        new_code = re.sub(
            r'\bprint\b',
            'print',
            new_code
        )

        if new_code != code:
            code = new_code
            stats["changes"] += 1

        # Remove redundant function definitions.
        new_code = remove_redundant_functions(
            code,
            evaluator
        )

        if new_code != code:
            code = new_code
            stats["changes"] += 1

        # Clean names only after resolution.
        new_code = clean_names(code)

        if new_code != code:
            code = new_code
            stats["changes"] += 1

        # If no changes -> stable.
        if code == before:
            break

    stats["functions"] = len(
        evaluator.functions
    )

    stats["constants"] = len(
        evaluator.env
    )

    code = final_cleanup(code)

    return code, stats


# ============================================================
# FINAL CLEANUP
# ============================================================

def final_cleanup(code):
    code = remove_comments(code)

    # Remove blank lines around beginning/end.
    code = re.sub(
        r"^\s*\n+",
        "",
        code
    )

    code = re.sub(
        r"\n+\s*$",
        "\n",
        code
    )

    # Spaces around commas.
    code = re.sub(
        r"\s*,\s*",
        ", ",
        code
    )

    # Operators.
    code = re.sub(
        r"\s*\.\.\s*",
        " .. ",
        code
    )

    code = re.sub(
        r"\s*=\s*",
        " = ",
        code
    )

    # Don't destroy == / >= etc.
    code = re.sub(
        r"\s*=\s*(?!=)",
        " = ",
        code
    )

    # Multiple spaces.
    code = re.sub(
        r"[ \t]+",
        " ",
        code
    )

    # Keep newlines.
    code = re.sub(
        r" *\n *",
        "\n",
        code
    )

    # Duplicate blank lines.
    code = re.sub(
        r"\n{3,}",
        "\n\n",
        code
    )

    # Simple indentation.
    lines = code.splitlines()

    result = []
    indent = 0

    for line in lines:
        line = line.strip()

        if not line:
            if result and result[-1] != "":
                result.append("")

            continue

        lower = line.lower()

        if (
            lower.startswith("end")
            or lower.startswith("until")
            or lower.startswith("else")
            or lower.startswith("elseif")
        ):
            indent = max(
                0,
                indent - 1
            )

        result.append(
            "    " * indent
            + line
        )

        # Opening blocks.
        if re.search(
            r"\bthen\s*$",
            line
        ):
            indent += 1

        elif re.search(
            r"\bdo\s*$",
            line
        ):
            indent += 1

        elif re.match(
            r"^(local\s+)?function\b",
            line
        ) and not line.endswith("end"):
            indent += 1

        if lower.startswith(
            "else"
        ) or lower.startswith(
            "elseif"
        ):
            indent += 1

    return "\n".join(result).strip() + "\n"


# ============================================================
# ANALYSIS
# ============================================================

def analyze_code(source):
    patterns = {
        "string.char": r"\bstring\s*\.\s*char\s*\(",
        "string.byte": r"\bstring\s*\.\s*byte\s*\(",
        "string.reverse": r"\bstring\s*\.\s*reverse\s*\(",
        "string.rep": r"\bstring\s*\.\s*rep\s*\(",
        "string.sub": r"\bstring\s*\.\s*sub\s*\(",
        "string.format": r"\bstring\s*\.\s*format\s*\(",
        "table.concat": r"\btable\s*\.\s*concat\s*\(",
        "table.insert": r"\btable\s*\.\s*insert\s*\(",
        "load": r"\bload\s*\(",
        "loadstring": r"\bloadstring\s*\(",
        "getfenv": r"\bgetfenv\s*\(",
        "setfenv": r"\bsetfenv\s*\(",
        "debug": r"\bdebug\s*\.",
        "os": r"\bos\s*\.",
        "io": r"\bio\s*\.",
        "string escapes": r"\\(?:x[0-9A-Fa-f]{2}|\d{1,3})",
        "hex numbers": r"\b0[xX][0-9A-Fa-f]+\b",
        "binary numbers": r"\b0[bB][01]+\b",
        "hex variable names": r"\b_0[xX][0-9A-Fa-f]+\b",
    }

    result = []

    for name, pattern in patterns.items():
        count = len(
            re.findall(
                pattern,
                source
            )
        )

        if count:
            result.append(
                (name, count)
            )

    return result


# ============================================================
# HTML
# ============================================================

def create_html(
    filename,
    original,
    result,
    found,
    stats
):
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
            "No known patterns"
            "</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>
Lua Deobfuscator
</title>

<style>
* {{
    box-sizing:border-box;
}}

body {{
    margin:0;
    background:#080a0f;
    color:#e9edf5;
    font-family:
        Inter,
        system-ui,
        sans-serif;
}}

.container {{
    width:min(1500px,94%);
    margin:30px auto;
}}

.header {{
    background:#11151c;
    border:1px solid #252b36;
    border-radius:22px;
    padding:26px;
    margin-bottom:20px;
}}

h1 {{
    margin:0;
    font-size:30px;
}}

.muted {{
    color:#8b95a5;
    margin-top:7px;
}}

.cards {{
    display:grid;
    grid-template-columns:
        repeat(auto-fit,minmax(180px,1fr));
    gap:14px;
    margin-bottom:20px;
}}

.card {{
    background:#11151c;
    border:1px solid #252b36;
    border-radius:18px;
    padding:20px;
}}

.value {{
    font-size:27px;
    font-weight:800;
}}

.label {{
    color:#7f8999;
    margin-top:5px;
}}

.panel {{
    background:#11151c;
    border:1px solid #252b36;
    border-radius:20px;
    margin-bottom:20px;
    overflow:hidden;
}}

.title {{
    padding:17px 20px;
    border-bottom:1px solid #252b36;
    font-weight:800;
}}

pre {{
    margin:0;
    padding:22px;
    overflow:auto;
    font-family:
        "JetBrains Mono",
        Consolas,
        monospace;
    font-size:13px;
    line-height:1.65;
    white-space:pre;
}}

table {{
    width:100%;
    border-collapse:collapse;
}}

td {{
    padding:13px 18px;
    border-bottom:1px solid #202631;
}}

td:last-child {{
    text-align:right;
    font-weight:800;
}}

.badge {{
    display:inline-block;
    margin-left:8px;
    padding:4px 8px;
    border-radius:8px;
    background:#1a202b;
    color:#9da8ba;
    font-size:11px;
}}

.footer {{
    text-align:center;
    color:#697383;
    padding:25px;
}}
</style>
</head>

<body>

<div class="container">

<div class="header">
<h1>Lua Static Deobfuscator</h1>
<div class="muted">
{html.escape(filename)}
</div>
</div>

<div class="cards">

<div class="card">
<div class="value">
{len(original.encode("utf-8")):,}
</div>
<div class="label">
Original bytes
</div>
</div>

<div class="card">
<div class="value">
{len(result.encode("utf-8")):,}
</div>
<div class="label">
Result bytes
</div>
</div>

<div class="card">
<div class="value">
{stats["rounds"]}
</div>
<div class="label">
Analysis rounds
</div>
</div>

<div class="card">
<div class="value">
{stats["changes"]}
</div>
<div class="label">
Transformations
</div>
</div>

<div class="card">
<div class="value">
{stats["functions"]}
</div>
<div class="label">
Functions discovered
</div>
</div>

<div class="card">
<div class="value">
{stats["constants"]}
</div>
<div class="label">
Constants discovered
</div>
</div>

</div>

<div class="panel">

<div class="title">
Detected obfuscation
</div>

<table>
{rows}
</table>

</div>

<div class="panel">

<div class="title">
Deobfuscated Lua
<span class="badge">
STATIC
</span>
</div>

<pre>{html.escape(result)}</pre>

</div>

<div class="panel">

<div class="title">
Original Lua
</div>

<pre>{html.escape(original)}</pre>

</div>

<div class="footer">
Lua Static Deobfuscator
</div>

</div>

</body>
</html>
"""


# ============================================================
# FILE READER
# ============================================================

def read_lua(path):
    data = Path(path).read_bytes()

    if len(data) > MAX_FILE_SIZE:
        raise ValueError(
            "File exceeds 10 MB"
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

    for encoding in encodings:
        try:
            text = data.decode(
                encoding
            )

            if (
                "\x00" in text
                and encoding not in {
                    "utf-16",
                    "utf-16-le",
                    "utf-16-be"
                }
            ):
                continue

            return text

        except UnicodeDecodeError:
            pass

    raise ValueError(
        "Unknown Lua file encoding"
    )


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "🧩 Lua Deobfuscator\n\n"
        "Отправь мне .lua файл.\n\n"
        "Я статически разбираю его, "
        "раскрываю доступные декодеры, "
        "константы, таблицы и выражения "
        "и возвращаю HTML.\n\n"
        "⚠️ Сам загруженный Lua-код "
        "не запускается."
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Поддерживаются статические конструкции:\n\n"
        "• функции-декодеры\n"
        "• local constants\n"
        "• таблицы\n"
        "• table[index]\n"
        "• #table\n"
        "• numeric for\n"
        "• string.char\n"
        "• string.byte\n"
        "• string.reverse\n"
        "• string.sub\n"
        "• string.rep\n"
        "• string.format\n"
        "• table.concat\n"
        "• table.insert\n"
        "• tonumber\n"
        "• tostring\n"
        "• арифметика\n"
        "• concatenation\n"
        "• static load generation\n"
        "• очистка _0x имен\n"
        "• повторный анализ"
    )


@dp.message(F.document)
async def document_handler(
    message: Message
):
    document = message.document

    if not document:
        return

    filename = (
        document.file_name
        or "script.lua"
    )

    extension = (
        Path(filename)
        .suffix
        .lower()
    )

    if extension not in {
        ".lua",
        ".txt"
    }:
        await message.answer(
            "❌ Нужен .lua или .txt файл."
        )
        return

    await message.answer(
        "🔬 Разбираю Lua...\n"
        "Файл не будет выполнен."
    )

    input_path = None
    output_path = None

    try:
        fd, input_path = tempfile.mkstemp(
            suffix=".lua"
        )

        os.close(fd)

        await bot.download(
            document,
            destination=input_path
        )

        original = read_lua(
            input_path
        )

        if not original.strip():
            raise ValueError(
                "Empty file"
            )

        result, stats = deobfuscate(
            original
        )

        found = analyze_code(
            original
        )

        stem = Path(
            filename
        ).stem

        stem = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            stem
        )

        html_name = (
            stem
            + "_deobfuscated.html"
        )

        output_path = str(
            Path(
                tempfile.gettempdir()
            ) / html_name
        )

        content = create_html(
            filename,
            original,
            result,
            found,
            stats
        )

        Path(
            output_path
        ).write_text(
            content,
            encoding="utf-8"
        )

        await message.answer_document(
            FSInputFile(
                output_path,
                filename=html_name
            ),
            caption=(
                "✅ Готово\n\n"
                f"📄 {filename}\n"
                f"🔄 Раундов: {stats['rounds']}\n"
                f"🧩 Функций: {stats['functions']}\n"
                f"🔢 Констант: {stats['constants']}\n"
                f"✨ Изменений: {stats['changes']}\n\n"
                "Lua-код не выполнялся."
            )
        )

    except Exception as e:
        logging.exception(
            "DEOBFUSCATION ERROR"
        )

        await message.answer(
            "❌ Ошибка:\n\n"
            f"{type(e).__name__}: {e}"
        )

    finally:
        for path in [
            input_path,
            output_path
        ]:
            if path:
                try:
                    Path(path).unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass


@dp.message()
async def fallback(
    message: Message
):
    await message.answer(
        "📎 Отправь Lua-файл "
        "как документ."
    )


# ============================================================
# RENDER HEALTH
# ============================================================

async def health(
    request
):
    return web.Response(
        text="OK"
    )


async def start_http():
    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/health",
        health
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logging.info(
        "HTTP server: %s",
        PORT
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    logging.info(
        "Starting advanced Lua deobfuscator"
    )

    await start_http()

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
