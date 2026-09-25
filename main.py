import os
import io
import re
import asyncio
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_FILE_SIZE = 10 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


# ============================================================
# RENDER HEALTH
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def health_server():
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ============================================================
# RANDOM NAMES
# ============================================================

ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def rnd_name(prefix="_"):
    return prefix + "".join(
        secrets.choice(ALPHABET)
        for _ in range(secrets.randbelow(8) + 12)
    )


# ============================================================
# PROTECTED LUAU / ROBLOX NAMES
# ============================================================

KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end",
    "false", "for", "function", "goto", "if", "in",
    "local", "nil", "not", "or", "repeat", "return",
    "then", "true", "until", "while", "continue",
    "type", "export",
}

PROTECTED = {
    "game",
    "workspace",
    "script",
    "shared",
    "_G",

    "Enum",
    "Instance",
    "Vector2",
    "Vector3",
    "Vector2int16",
    "Vector3int16",
    "CFrame",
    "Color3",
    "BrickColor",
    "UDim",
    "UDim2",
    "Ray",
    "Random",
    "TweenInfo",

    "task",
    "coroutine",
    "debug",
    "math",
    "string",
    "table",
    "bit32",
    "utf8",
    "os",

    "pairs",
    "ipairs",
    "next",
    "select",
    "unpack",

    "pcall",
    "xpcall",
    "error",
    "assert",
    "warn",
    "print",
    "tostring",
    "tonumber",
    "type",
    "typeof",

    "rawget",
    "rawset",
    "rawequal",
    "rawlen",
    "setmetatable",
    "getmetatable",

    "require",

    "wait",
    "spawn",
    "delay",

    # common executor/environment functions
    "getgenv",
    "getrenv",
    "getsenv",
    "getgc",
    "gethui",
    "getconnections",
    "hookfunction",
    "hookmetamethod",
    "newcclosure",
    "checkcaller",
    "iscclosure",
    "islclosure",
    "identifyexecutor",
    "setclipboard",

    "request",
    "http_request",
    "syn",
    "fluxus",
    "krnl",

    # Roblox services
    "Players",
    "ReplicatedStorage",
    "ReplicatedFirst",
    "ServerScriptService",
    "ServerStorage",
    "StarterGui",
    "StarterPlayer",
    "Lighting",
    "RunService",
    "UserInputService",
    "TweenService",
    "HttpService",
    "TeleportService",
    "CoreGui",
    "CollectionService",
    "MarketplaceService",
    "VirtualInputManager",
}


# ============================================================
# LONG BRACKET STRING
# ============================================================

def read_long_bracket(src, pos):
    if pos >= len(src) or src[pos] != "[":
        return None

    i = pos + 1
    eq = 0

    while i < len(src) and src[i] == "=":
        eq += 1
        i += 1

    if i >= len(src) or src[i] != "[":
        return None

    close = "]" + ("=" * eq) + "]"

    end = src.find(close, i + 1)

    if end == -1:
        return None

    return end + len(close)


# ============================================================
# LEXER
# ============================================================

def tokenize(src):
    tokens = []

    i = 0
    n = len(src)

    while i < n:

        c = src[i]

        # ----------------------------------------------------
        # whitespace
        # ----------------------------------------------------

        if c.isspace():
            j = i + 1

            while j < n and src[j].isspace():
                j += 1

            tokens.append(("ws", src[i:j]))
            i = j
            continue

        # ----------------------------------------------------
        # comments
        # ----------------------------------------------------

        if c == "-" and i + 1 < n and src[i + 1] == "-":

            long_end = read_long_bracket(src, i + 2)

            if long_end:
                tokens.append(("comment", src[i:long_end]))
                i = long_end
                continue

            j = i + 2

            while j < n and src[j] not in "\r\n":
                j += 1

            tokens.append(("comment", src[i:j]))

            i = j
            continue

        # ----------------------------------------------------
        # quoted string
        # ----------------------------------------------------

        if c in ("'", '"'):

            quote = c
            j = i + 1

            while j < n:

                if src[j] == "\\":
                    j += 2
                    continue

                if src[j] == quote:
                    j += 1
                    break

                j += 1

            tokens.append(("string", src[i:j]))

            i = j
            continue

        # ----------------------------------------------------
        # long string
        # ----------------------------------------------------

        if c == "[":

            end = read_long_bracket(src, i)

            if end:
                tokens.append(("longstring", src[i:end]))
                i = end
                continue

        # ----------------------------------------------------
        # identifier
        # ----------------------------------------------------

        if c.isalpha() or c == "_":

            j = i + 1

            while j < n and (
                src[j].isalnum() or src[j] == "_"
            ):
                j += 1

            value = src[i:j]

            if value in KEYWORDS:
                tokens.append(("keyword", value))
            else:
                tokens.append(("identifier", value))

            i = j
            continue

        # ----------------------------------------------------
        # number
        # ----------------------------------------------------

        if c.isdigit() or (
            c == "." and
            i + 1 < n and
            src[i + 1].isdigit()
        ):

            j = i

            while j < n:
                ch = src[j]

                if ch.isalnum() or ch in "._":
                    j += 1
                    continue

                if ch in "+-" and j > i:
                    if src[j - 1] in "eE":
                        j += 1
                        continue

                break

            tokens.append(("number", src[i:j]))

            i = j
            continue

        # ----------------------------------------------------
        # operators
        # ----------------------------------------------------

        operators = (
            "...",
            "//=",
            "..=",
            "==",
            "~=",
            "<=",
            ">=",
            "//",
            "..",
            "::",
            "+=",
            "-=",
            "*=",
            "/=",
            "%=",
            "^=",
        )

        found = None

        for op in operators:
            if src.startswith(op, i):
                found = op
                break

        if found:
            tokens.append(("symbol", found))
            i += len(found)
            continue

        tokens.append(("symbol", c))
        i += 1

    return tokens


# ============================================================
# COMMENTS
# ============================================================

def strip_comments(tokens):
    result = []

    for kind, value in tokens:

        if kind == "comment":
            if "\n" in value:
                result.append(("ws", "\n"))
            continue

        result.append((kind, value))

    return result


# ============================================================
# STRING DECODER
# ============================================================

def decode_string(raw):
    if len(raw) < 2:
        return None

    quote = raw[0]

    if quote not in ("'", '"'):
        return None

    if raw[-1] != quote:
        return None

    s = raw[1:-1]

    data = bytearray()

    i = 0

    while i < len(s):

        c = s[i]

        if c != "\\":
            data.extend(c.encode("utf-8"))
            i += 1
            continue

        i += 1

        if i >= len(s):
            return None

        c = s[i]

        simple = {
            "a": 7,
            "b": 8,
            "f": 12,
            "n": 10,
            "r": 13,
            "t": 9,
            "v": 11,
            "\\": 92,
            "'": 39,
            '"': 34,
        }

        if c in simple:
            data.append(simple[c])
            i += 1
            continue

        # decimal escape
        if c.isdigit():

            digits = c
            i += 1

            for _ in range(2):

                if i < len(s) and s[i].isdigit():
                    digits += s[i]
                    i += 1
                else:
                    break

            value = int(digits)

            if value > 255:
                return None

            data.append(value)
            continue

        # hexadecimal escape
        if c == "x":

            if i + 2 >= len(s):
                return None

            hx = s[i + 1:i + 3]

            if not re.fullmatch(
                r"[0-9a-fA-F]{2}",
                hx
            ):
                return None

            data.append(int(hx, 16))
            i += 3
            continue

        # unknown escape
        data.append(92)

        data.extend(c.encode("utf-8"))

        i += 1

    return bytes(data)


# ============================================================
# STRING POOL
# ============================================================

def build_string_runtime():

    fn = rnd_name("_decode")

    runtime = (
        f"local {fn}=function(_a)"
        f"local _r={{}};"
        f"for _i=1,#_a do "
        f"_r[_i]=string.char(_a[_i]) "
        f"end;"
        f"return table.concat(_r)"
        f"end;"
    )

    return fn, runtime


def obfuscate_strings(tokens):

    decoder, runtime = build_string_runtime()

    result = []
    changed = False

    for kind, value in tokens:

        if kind != "string":
            result.append((kind, value))
            continue

        data = decode_string(value)

        if data is None:
            result.append((kind, value))
            continue

        if len(data) == 0:
            result.append(("raw", '""'))
            continue

        # Every string gets a different permutation.
        indexed = list(enumerate(data))

        # Fisher-Yates-like random order.
        order = list(range(len(indexed)))
        secrets.SystemRandom().shuffle(order)

        shuffled = [data[i] for i in order]

        # Encode permutation too.
        positions = [0] * len(order)

        for new_pos, old_pos in enumerate(order):
            positions[old_pos] = new_pos + 1

        arr = ",".join(str(x) for x in shuffled)
        perm = ",".join(str(x) for x in positions)

        # Runtime reconstruction:
        #
        # local a={...}
        # local p={...}
        # local r={}
        # for i=1,#p do r[i]=a[p[i]] end
        # decoder(r)
        #
        # This avoids a fixed XOR key.

        a = rnd_name("_a")
        p = rnd_name("_p")
        r = rnd_name("_r")

        expression = (
            f"(function()"
            f"local {a}={{{arr}}};"
            f"local {p}={{{perm}}};"
            f"local {r}={{}};"
            f"for _i=1,#{p} do "
            f"{r}[_i]={a}[{p}[_i]] "
            f"end;"
            f"return {decoder}({r})"
            f"end)()"
        )

        result.append(("raw", expression))

        changed = True

    if changed:
        result.insert(0, ("raw", runtime))

    return result


# ============================================================
# NUMBER ENCODING
# ============================================================

DECIMAL_INTEGER = re.compile(r"^[0-9]+$")


def encode_numbers(tokens):

    result = []

    for index, (kind, value) in enumerate(tokens):

        if kind != "number":
            result.append((kind, value))
            continue

        # Do not modify complicated numeric syntax.
        if not DECIMAL_INTEGER.fullmatch(value):
            result.append((kind, value))
            continue

        # Don't touch very small literals.
        if value in ("0", "1", "2"):
            result.append((kind, value))
            continue

        try:
            number = int(value)

            # Safe exact representation.
            #
            # Instead of:
            # 123456
            #
            # produce:
            # (123456 + 0)
            #
            # but with random split:
            # (900 + 122556)
            #
            # Both are mathematically exact integers.

            a = secrets.randbelow(
                min(abs(number), 100000) + 1
            )

            if number >= 0:
                b = number - a
            else:
                a = -a
                b = number - a

            expression = f"({a}+{b})"

            result.append(("raw", expression))

        except Exception:
            result.append((kind, value))

    return result


# ============================================================
# LOCAL DISCOVERY
# ============================================================

def find_local_declarations(tokens):
    """
    Finds simple local declarations.

    It intentionally does NOT attempt to understand every
    possible Luau scope. The transformer therefore only
    renames identifiers when it can do so conservatively.
    """

    names = []

    i = 0

    while i < len(tokens):

        kind, value = tokens[i]

        if kind == "keyword" and value == "local":

            j = i + 1

            # local function foo
            if (
                j < len(tokens)
                and tokens[j][0] == "keyword"
                and tokens[j][1] == "function"
            ):
                j += 1

                if (
                    j < len(tokens)
                    and tokens[j][0] == "identifier"
                ):
                    name = tokens[j][1]

                    if name not in PROTECTED:
                        names.append(name)

                i = j
                continue

            # local a,b,c
            while j < len(tokens):

                tk, tv = tokens[j]

                if tk == "identifier":

                    if tv not in PROTECTED:
                        names.append(tv)

                    j += 1
                    continue

                if tv == ",":
                    j += 1
                    continue

                break

            i = j
            continue

        i += 1

    return list(dict.fromkeys(names))


# ============================================================
# CONSERVATIVE LOCAL RENAMER
# ============================================================

def rename_locals(tokens):

    declared = find_local_declarations(tokens)

    if not declared:
        return tokens

    mapping = {}

    for name in declared:
        mapping[name] = rnd_name("_v")

    result = []

    for kind, value in tokens:

        if (
            kind == "identifier"
            and value in mapping
            and value not in PROTECTED
        ):
            result.append(
                ("identifier", mapping[value])
            )
        else:
            result.append((kind, value))

    return result


# ============================================================
# SAFE CONSTANT POOL
# ============================================================

def add_constant_noise():

    n1 = secrets.randbelow(900000) + 100000
    n2 = secrets.randbelow(900000) + 100000

    a = rnd_name("_c")
    b = rnd_name("_c")

    return (
        f"local {a}=({n1}+0);"
        f"local {b}=({n2}+0);"
    )


# ============================================================
# MINIFIER
# ============================================================

def render(tokens):

    output = []

    previous = None

    for kind, value in tokens:

        if kind == "ws":
            continue

        if kind == "raw":
            value = value

        # Prevent:
        # localfoo
        # foo123
        # 1abc
        #
        # from accidentally joining.

        if output and previous:

            prev_kind, prev_value = previous

            if (
                prev_kind in (
                    "identifier",
                    "keyword",
                    "number",
                )
                and kind in (
                    "identifier",
                    "keyword",
                    "number",
                )
            ):
                output.append(" ")

        output.append(value)

        previous = (kind, value)

    return "".join(output)


# ============================================================
# SAFE JUNK
# ============================================================

def junk_block():

    x = rnd_name("_j")
    y = rnd_name("_j")
    z = rnd_name("_j")

    a = secrets.randbelow(100000) + 10000
    b = secrets.randbelow(100000) + 10000

    return (
        f"local {x}={a};"
        f"local {y}={b};"
        f"local {z}=({x}+{y}-{x});"
    )


# ============================================================
# MAX OBFUSCATOR
# ============================================================

def max_obfuscate(source):

    if not source.strip():
        raise ValueError("Empty source")

    # Preserve shebang.
    shebang = ""

    if source.startswith("#!"):
        pos = source.find("\n")

        if pos >= 0:
            shebang = source[:pos + 1]
            source = source[pos + 1:]
        else:
            shebang = source
            source = ""

    # --------------------------------------------------------
    # TOKENIZE
    # --------------------------------------------------------

    tokens = tokenize(source)

    # --------------------------------------------------------
    # REMOVE COMMENTS
    # --------------------------------------------------------

    tokens = strip_comments(tokens)

    # --------------------------------------------------------
    # STRINGS
    # --------------------------------------------------------

    tokens = obfuscate_strings(tokens)

    # --------------------------------------------------------
    # NUMBERS
    # --------------------------------------------------------

    tokens = encode_numbers(tokens)

    # --------------------------------------------------------
    # LOCALS
    # --------------------------------------------------------

    tokens = rename_locals(tokens)

    # --------------------------------------------------------
    # RENDER
    # --------------------------------------------------------

    result = render(tokens)

    # --------------------------------------------------------
    # SAFE RANDOM CONSTANTS
    # --------------------------------------------------------

    result = add_constant_noise() + result

    # --------------------------------------------------------
    # SAFE JUNK
    # --------------------------------------------------------

    result = junk_block() + result

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------

    marker = secrets.token_hex(12)

    header = (
        f"--[[ protected:{marker} ]]--"
    )

    return (
        shebang
        + header
        + result
    )


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🔐 MAX Luau Obfuscator\n\n"
        "Отправь мне файл .lua.\n\n"
        "Обфускация автоматически выполняется "
        "в максимальном режиме.\n\n"
        "Никаких режимов выбирать не нужно."
    )


async def process_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    document = update.message.document

    if not document:
        return

    filename = document.file_name or "script.lua"

    if not filename.lower().endswith(".lua"):

        await update.message.reply_text(
            "❌ Нужен файл .lua"
        )

        return

    if (
        document.file_size
        and document.file_size > MAX_FILE_SIZE
    ):

        await update.message.reply_text(
            "❌ Максимальный размер файла: 10 MB"
        )

        return

    status = await update.message.reply_text(
        "⏳ Анализирую Luau...\n"
        "🔐 Выполняю MAX-обфускацию..."
    )

    try:

        tg_file = await document.get_file()

        source_buffer = io.BytesIO()

        await tg_file.download_to_memory(
            source_buffer
        )

        source_buffer.seek(0)

        raw = source_buffer.read()

        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            source = raw.decode(
                "utf-8",
                errors="replace"
            )

        # CPU work
        loop = asyncio.get_running_loop()

        output = await loop.run_in_executor(
            None,
            max_obfuscate,
            source,
        )

        result = io.BytesIO(
            output.encode("utf-8")
        )

        base = os.path.splitext(
            filename
        )[0]

        result.name = (
            base
            + "_MAX_OBF.lua"
        )

        result.seek(0)

        await status.delete()

        await update.message.reply_document(
            document=result,
            caption=(
                "✅ MAX обфускация завершена\n\n"
                "🔐 Строки: encoded\n"
                "🔢 Константы: transformed\n"
                "🔤 Locals: randomized\n"
                "🧩 Structure: minified\n"
                "🛡 Runtime loader: отсутствует\n\n"
                "📄 "
                + result.name
            )
        )

    except Exception as e:

        try:
            await status.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Ошибка:\n\n"
            + str(e)[:2000]
        )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📄 Просто отправь файл .lua"
    )


async def error_handler(update, context):

    print(
        "BOT ERROR:",
        repr(context.error)
    )


# ============================================================
# BOT START
# ============================================================

def run_bot():

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_file
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )

    app.add_error_handler(
        error_handler
    )

    print(
        "MAX Luau Obfuscator started"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":

    threading.Thread(
        target=health_server,
        daemon=True
    ).start()

    run_bot()
