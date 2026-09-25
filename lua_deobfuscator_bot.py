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
# REAL EXECUTION ENGINE (replaces the old text-pattern guesser)
# ============================================================
#
# The previous version of this bot tried to figure out what obfuscated
# Lua code "means" by scanning it with regular expressions and manually
# re-implementing tiny bits of the Lua semantics in Python. That approach
# is fundamentally limited: any obfuscation trick the author didn't
# specifically special-case (a slightly different string.char pattern, a
# custom XOR loop, a nested loadstring, table-based dispatch, etc.) just
# silently fails to resolve.
#
# This version instead ACTUALLY RUNS the code, for real, inside a
# locked-down sandbox built on the real Lua 5.4 interpreter (compiled
# from the system's liblua5.4 at deploy time -- see ensure_runtime()
# below). A debug-hook based tracer records the real, executed value of
# every local variable and every print()/warn() call as the script runs.
# Because the VM itself is doing the decoding, any obfuscation trick
# resolves automatically -- there is nothing to pattern-match.
#
# Safety model:
#   - io, os.execute/remove/rename, require, package, dofile, loadfile
#     are NOT exposed to the sandboxed script.
#   - Roblox/executor-specific globals (game, workspace, Instance, task,
#     Drawing, hookfunction, getgenv, request, writefile, ...) are stubbed
#     as harmless no-ops, so executor scripts run far enough to reveal
#     their decoded strings/logic instead of crashing on an undefined
#     global -- but they can't touch a real filesystem, network, or OS.
#   - Hard wall-clock timeout (protects against obfuscated infinite loops).
#   - The uploaded code is NEVER executed unsandboxed and never touches
#     the bot's own filesystem/network.

import shutil
import subprocess

import real_deobfuscator as _real_deob

RUNTIME_DIR = Path(tempfile.gettempdir()) / "lua_deobf_runtime"


# ------------------------------------------------------------------
# Kept from the original static analyzer: strips comments before the
# code is handed to the real Lua sandbox. Not strictly required (Lua
# itself ignores comments), but it keeps the "before/after" diff in the
# generated HTML report focused on real code rather than commentary.
# ------------------------------------------------------------------
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


def _compile_runlua(dest_bin: Path) -> None:
    """
    Builds the tiny 'runlua' launcher against the system's liblua5.4 at
    first startup. We don't ship a prebuilt binary because the exact
    .so path/soname can differ between hosts -- compiling once against
    whatever is actually installed is more portable.
    """
    c_source = RUNTIME_DIR / "runlua.c"
    c_source.write_text(r"""
#include <stdio.h>
#include <string.h>
typedef struct lua_State lua_State;
extern lua_State *luaL_newstate(void);
extern void luaL_openlibs(lua_State *L);
extern int luaL_loadfilex(lua_State *L, const char *filename, const char *mode);
extern int lua_pcallk(lua_State *L, int nargs, int nresults, int errfunc, long ctx, void *k);
extern const char *lua_tolstring(lua_State *L, int idx, size_t *len);
extern void lua_close(lua_State *L);
extern void lua_createtable(lua_State *L, int narr, int nrec);
extern void lua_pushlstring(lua_State *L, const char *s, size_t len);
extern void lua_seti(lua_State *L, int idx, long long n);
extern void lua_setglobal(lua_State *L, const char *name);
#define LUA_OK 0
#define LUA_MULTRET (-1)
int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s file.lua [args...]\n", argv[0]); return 1; }
    lua_State *L = luaL_newstate();
    luaL_openlibs(L);
    lua_createtable(L, argc - 2 > 0 ? argc - 2 : 0, 0);
    for (int i = 2; i < argc; i++) {
        lua_pushlstring(L, argv[i], strlen(argv[i]));
        lua_seti(L, -2, i - 1);
    }
    lua_setglobal(L, "arg");
    int r = luaL_loadfilex(L, argv[1], NULL);
    if (r != LUA_OK) { fprintf(stderr, "LOAD ERROR: %s\n", lua_tolstring(L, -1, NULL)); return 2; }
    for (int i = 2; i < argc; i++) lua_pushlstring(L, argv[i], strlen(argv[i]));
    r = lua_pcallk(L, argc - 2 > 0 ? argc - 2 : 0, LUA_MULTRET, 0, 0, NULL);
    if (r != LUA_OK) { fprintf(stderr, "RUNTIME ERROR: %s\n", lua_tolstring(L, -1, NULL)); return 3; }
    lua_close(L);
    return 0;
}
""", encoding="utf-8")

    so_candidates = [
        p for p in list(Path("/usr/lib").rglob("liblua5.*.so*"))
                 + list(Path("/usr/lib").rglob("liblua.so*"))
        if "-c++" not in p.name
    ]
    if not so_candidates:
        # fall back to anything at all, including the C++ variant, rather
        # than failing outright -- better to try than to give up.
        so_candidates = list(Path("/usr/lib").rglob("liblua5.*.so*"))
        so_candidates += list(Path("/usr/lib").rglob("liblua.so*"))
    if not so_candidates:
        raise RuntimeError(
            "No system liblua*.so found -- install liblua5.4-0 "
            "(or equivalent) in the deployment image."
        )
    # Prefer the most specific/versioned .so (e.g. liblua5.4.so.0.0.0)
    so_candidates.sort(key=lambda p: len(str(p)), reverse=True)
    so_path = str(so_candidates[0])

    subprocess.run(
        ["gcc", str(c_source), "-o", str(dest_bin), so_path],
        check=True,
        capture_output=True,
    )


_SANDBOX_RUNNER_SOURCE = None  # filled in by ensure_runtime()


def ensure_runtime() -> tuple[str, str]:
    """
    Idempotently prepares the real-Lua sandbox runtime on first use:
      1. compiles ./runlua against the system's liblua5.4 if missing
      2. writes out sandbox_runner.lua (the tracer+sandbox) if missing
    Returns (runlua_path, sandbox_runner_path).
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    runlua_bin = RUNTIME_DIR / "runlua"
    runner_lua = RUNTIME_DIR / "sandbox_runner.lua"

    if not runlua_bin.exists():
        _compile_runlua(runlua_bin)

    if not runner_lua.exists():
        # Shipped alongside this file.
        shipped = Path(__file__).parent / "sandbox_runner.lua"
        shutil.copy(shipped, runner_lua)

    return str(runlua_bin), str(runner_lua)


def deobfuscate(source):
    """
    Public entry point used by the Telegram handler below. Same
    (result, stats) shape the rest of the bot already expects, so no
    other code in this file needs to change.
    """
    runlua_path, runner_path = ensure_runtime()
    _real_deob.RUNLUA_BIN = runlua_path
    _real_deob.SANDBOX_RUNNER = runner_path

    code = remove_comments(source)
    return _real_deob.deobfuscate(code)


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
REAL EXECUTION
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
        "🧩 Lua Deobfuscator (real execution engine)\n\n"
        "Отправь мне .lua файл.\n\n"
        "Я РЕАЛЬНО выполняю код внутри изолированной "
        "песочницы (настоящий интерпретатор Lua 5.4) "
        "и показываю фактические, вычисленные значения "
        "переменных и весь print()/warn() вывод — "
        "а не догадки по regex.\n\n"
        "🔒 Песочница: нет доступа к файловой системе, "
        "сети, os.execute и т.д. Roblox/executor-функции "
        "(game, workspace, Instance, task, hookfunction...) "
        "заменены безопасными заглушками, поэтому скрипт "
        "не может ничего реально сломать, но при этом "
        "выполняется достаточно, чтобы раскрыть все "
        "декодированные строки и логику.\n\n"
        "⏱ Жёсткий таймаут на случай бесконечных циклов."
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Как это работает:\n\n"
        "• Код выполняется целиком настоящим Lua 5.4\n"
        "• debug.sethook трассирует каждое присвоение "
        "локальной переменной и каждый print/warn\n"
        "• Любые трюки (string.char+арифметика, XOR, "
        "reverse, вложенный load()/loadstring(), "
        "table-based dispatch и т.д.) разворачиваются "
        "сами — потому что их реально исполняет VM, "
        "а не пытается угадать парсер\n"
        "• Результат: исходный код с комментариями вида "
        "--[[ RUNTIME: _0xG = \"hello\" ]] рядом с каждой "
        "строкой, плюс отдельная секция реального "
        "программного вывода\n\n"
        "Ограничения:\n"
        "• Roblox-специфичные вызовы (game:GetService, "
        "RemoteEvent и т.п.) — безопасные заглушки, "
        "они не взаимодействуют с реальной игрой\n"
        "• Бесконечные циклы прерываются по таймауту — "
        "всё, что успело выполниться до этого момента, "
        "всё равно попадёт в отчёт"
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
        "🔬 Выполняю Lua в песочнице...\n"
        "Реальный запуск, изолированно, с таймаутом."
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
                f"🧠 Движок: {stats.get('engine', 'real-lua-5.4-sandbox')}\n"
                f"🧩 Функций отслежено: {stats['functions']}\n"
                f"🔢 Констант раскрыто: {stats['constants']}\n"
                f"✨ Событий трассировки: {stats['changes']}\n\n"
                "Код был реально выполнен в изолированной "
                "песочнице (не на реальной игре/системе)."
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