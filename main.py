import os
import re
import io
import ast
import asyncio
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

MAX_FILE_SIZE = 5 * 1024 * 1024

# user_id -> selected mode
USER_MODE = {}

# ============================================================
# RENDER HEALTH SERVER
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
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# ============================================================
# SAFE RANDOM NAME
# ============================================================

def random_name(prefix="_"):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(14))


# ============================================================
# LUA STRING PARSER
# ============================================================

def decode_lua_string(raw):
    """
    Converts the content of a normal Lua quoted string into bytes.

    Supports:
      \\n
      \\r
      \\t
      \\b
      \\f
      \\v
      \\\\
      \\"
      \\'
      \\ddd
      \\xXX

    If something unusual is encountered, the original
    literal is returned unchanged by the caller.
    """

    if len(raw) < 2:
        return None

    quote = raw[0]

    if quote not in ("'", '"') or raw[-1] != quote:
        return None

    s = raw[1:-1]
    out = bytearray()

    i = 0

    while i < len(s):
        c = s[i]

        if c != "\\":
            encoded = c.encode("utf-8")
            out.extend(encoded)
            i += 1
            continue

        i += 1

        if i >= len(s):
            return None

        c = s[i]

        escapes = {
            "a": 7,
            "b": 8,
            "f": 12,
            "n": 10,
            "r": 13,
            "t": 9,
            "v": 11,
            "\\": 92,
            '"': 34,
            "'": 39,
        }

        if c in escapes:
            out.append(escapes[c])
            i += 1
            continue

        # \ddd
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

            out.append(value)
            continue

        # \xXX
        if c == "x":
            if i + 2 >= len(s):
                return None

            hx = s[i + 1:i + 3]

            if not re.fullmatch(r"[0-9a-fA-F]{2}", hx):
                return None

            out.append(int(hx, 16))
            i += 3
            continue

        # escaped newline
        if c == "\n":
            out.append(10)
            i += 1
            continue

        if c == "\r":
            if i + 1 < len(s) and s[i + 1] == "\n":
                i += 1

            out.append(10)
            i += 1
            continue

        # Unknown escape:
        # preserve the backslash literally.
        out.append(ord("\\"))
        encoded = c.encode("utf-8")
        out.extend(encoded)
        i += 1

    return bytes(out)


# ============================================================
# STRING ENCODING
# ============================================================

def string_expression(data, variable):
    """
    Creates a runtime expression that reconstructs a string.

    It doesn't use load/loadstring.
    """

    numbers = ",".join(str(x) for x in data)

    return (
        f"(function({variable})"
        f"local _t={{}};"
        f"for _i=1,#{variable} do "
        f"_t[_i]=string.char({variable}[_i]) "
        f"end;"
        f"return table.concat(_t)"
        f"end)({{{numbers}}})"
    )


# ============================================================
# LEXER
# ============================================================

IDENTIFIER_START = re.compile(r"[A-Za-z_]")
IDENTIFIER_BODY = re.compile(r"[A-Za-z0-9_]")

KEYWORDS = {
    "and",
    "break",
    "do",
    "else",
    "elseif",
    "end",
    "false",
    "for",
    "function",
    "goto",
    "if",
    "in",
    "local",
    "nil",
    "not",
    "or",
    "repeat",
    "return",
    "then",
    "true",
    "until",
    "while",
    "continue",
    "type",
    "export",
}

# Roblox/Luau names that must never be renamed.
PROTECTED_NAMES = {
    "game",
    "workspace",
    "script",
    "shared",
    "_G",

    "Enum",
    "Instance",
    "Vector2",
    "Vector3",
    "CFrame",
    "Color3",
    "UDim",
    "UDim2",
    "BrickColor",
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

    "http",
    "request",
    "http_request",
    "syn",
    "fluxus",
    "krnl",

    "Players",
    "LocalPlayer",
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
}


def is_identifier_start(c):
    return bool(c) and bool(IDENTIFIER_START.match(c))


def is_identifier_char(c):
    return bool(c) and bool(IDENTIFIER_BODY.match(c))


# ============================================================
# LONG STRING DETECTION
# ============================================================

def read_long_bracket(src, pos):
    """
    Reads Lua long strings/comments:

    [[ ... ]]
    [=[ ... ]=]
    [==[ ... ]==]

    Returns (end_position, content) or None.
    """

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

    return end + len(close), src[i + 1:end]


# ============================================================
# TOKENIZER
# ============================================================

def tokenize_luau(src):
    """
    Conservative Luau tokenizer.

    Tokens:
      whitespace
      comments
      strings
      longstrings
      identifiers
      numbers
      operators
      punctuation
      other
    """

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

            # long comment
            long_result = read_long_bracket(src, i + 2)

            if long_result:
                end, _ = long_result
                tokens.append(("comment", src[i:end]))
                i = end
                continue

            # normal comment
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

            long_result = read_long_bracket(src, i)

            if long_result:
                end, _ = long_result
                tokens.append(("longstring", src[i:end]))
                i = end
                continue

        # ----------------------------------------------------
        # identifier
        # ----------------------------------------------------

        if is_identifier_start(c):

            j = i + 1

            while j < n and is_identifier_char(src[j]):
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

            # hex
            if src.startswith(("0x", "0X"), i):
                j += 2

                while j < n and (
                    src[j].isdigit()
                    or src[j].lower() in "abcdef"
                    or src[j] == "_"
                ):
                    j += 1

            else:
                while j < n and (
                    src[j].isalnum()
                    or src[j] in "._+-"
                ):
                    # stop obvious operator sequence
                    if src[j] in "+-" and j > i:
                        prev = src[j - 1]

                        if prev not in "eE":
                            break

                    j += 1

            tokens.append(("number", src[i:j]))
            i = j
            continue

        # ----------------------------------------------------
        # operators
        # ----------------------------------------------------

        operators = (
            "...",
            "//",
            "..",
            "==",
            "~=",
            "<=",
            ">=",
            "::",
            "+=",
            "-=",
            "*=",
            "/=",
            "%=",
            "^=",
            "..=",
            "->",
        )

        found = None

        for op in operators:
            if src.startswith(op, i):
                found = op
                break

        if found:
            tokens.append(("operator", found))
            i += len(found)
            continue

        # ----------------------------------------------------
        # single char
        # ----------------------------------------------------

        tokens.append(("symbol", c))
        i += 1

    return tokens


# ============================================================
# COMMENT REMOVAL
# ============================================================

def remove_comments(tokens):
    result = []

    for kind, value in tokens:

        if kind == "comment":
            # Preserve newlines so line-related syntax remains sane.
            newlines = value.count("\n")

            if newlines:
                result.append(("ws", "\n" * newlines))

            continue

        result.append((kind, value))

    return result


# ============================================================
# STRING OBFUSCATION
# ============================================================

def obfuscate_strings(tokens):
    result = []

    runtime_name = random_name("_s")

    # One shared decoder.
    decoder = (
        f"local {runtime_name}=function(_a)"
        f"local _r={{}};"
        f"for _i=1,#_a do "
        f"_r[_i]=string.char(_a[_i]) "
        f"end;"
        f"return table.concat(_r)"
        f"end;"
    )

    inserted = False

    for kind, value in tokens:

        if kind == "string":

            decoded = decode_lua_string(value)

            if decoded is None:
                result.append((kind, value))
                continue

            # Empty strings are safe as-is.
            if len(decoded) == 0:
                result.append(("raw", '""'))
                continue

            numbers = ",".join(str(x) for x in decoded)

            expression = f"{runtime_name}({{{numbers}}})"

            result.append(("raw", expression))

            inserted = True

        else:
            result.append((kind, value))

    if inserted:
        result.insert(0, ("raw", decoder))

    return result


# ============================================================
# NUMBER OBFUSCATION
# ============================================================

SAFE_DECIMAL = re.compile(
    r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"
)


def obfuscate_numbers(tokens):
    result = []

    for kind, value in tokens:

        if kind != "number":
            result.append((kind, value))
            continue

        # Don't touch hex, scientific notation,
        # malformed-looking literals, etc.
        if not SAFE_DECIMAL.match(value):
            result.append((kind, value))
            continue

        # Don't touch 0 / 1 in potentially sensitive contexts.
        if value in ("0", "1"):
            result.append((kind, value))
            continue

        try:
            if "." in value:
                number = float(value)

                # Safe arithmetic expression.
                a = secrets.randbelow(50) + 2
                b = number - a

                replacement = f"({a}+({b!r}))"

            else:
                number = int(value)

                a = secrets.randbelow(1000) + 2
                b = number - a

                replacement = f"({a}+({b}))"

            result.append(("raw", replacement))

        except Exception:
            result.append((kind, value))

    return result


# ============================================================
# WHITESPACE MINIFIER
# ============================================================

def minify_tokens(tokens):
    output = []

    previous_kind = None
    previous_value = ""

    for kind, value in tokens:

        if kind == "ws":

            # Keep only necessary separation.
            if not output:
                continue

            # Newline isn't required after comments because
            # comments have already been removed.
            continue

        # Avoid joining identifiers together.
        if (
            output
            and previous_kind in ("identifier", "keyword", "number")
            and kind in ("identifier", "keyword", "number")
        ):
            output.append(" ")

        output.append(value)

        previous_kind = kind
        previous_value = value

    return "".join(output)


# ============================================================
# BASIC LOCAL RENAMING
# ============================================================

def rename_simple_locals(tokens):
    """
    Conservative local renaming.

    This deliberately only renames simple declarations:

        local foo = ...
        local foo, bar = ...

    and local function declarations:

        local function foo(...)

    It does NOT attempt dangerous global or member renaming.
    """

    mapping = {}

    result = []

    i = 0

    while i < len(tokens):

        kind, value = tokens[i]

        # local declaration
        if kind == "keyword" and value == "local":

            result.append((kind, value))
            i += 1

            # local function name
            if (
                i + 1 < len(tokens)
                and tokens[i][0] == "keyword"
                and tokens[i][1] == "function"
            ):
                result.append(tokens[i])
                i += 1

                if i < len(tokens) and tokens[i][0] == "identifier":
                    old = tokens[i][1]

                    if old not in PROTECTED_NAMES:
                        new = random_name("_l")
                        mapping[old] = new
                        result.append(("identifier", new))
                    else:
                        result.append(tokens[i])

                    i += 1

                continue

            # regular local names
            while i < len(tokens):

                tk, tv = tokens[i]

                if tk == "identifier":

                    if tv not in PROTECTED_NAMES:
                        if tv not in mapping:
                            mapping[tv] = random_name("_l")

                        result.append(("identifier", mapping[tv]))
                    else:
                        result.append(tokens[i])

                    i += 1
                    continue

                # comma means another local variable
                if tv == ",":
                    result.append(tokens[i])
                    i += 1
                    continue

                break

            continue

        result.append(tokens[i])
        i += 1

    # Apply only to identifiers outside protected names.
    final = []

    for kind, value in result:

        if (
            kind == "identifier"
            and value in mapping
        ):
            final.append(("identifier", mapping[value]))
        else:
            final.append((kind, value))

    return final


# ============================================================
# JUNK THAT DOES NOT AFFECT EXECUTION
# ============================================================

def add_safe_junk(source):
    """
    Adds completely isolated local constants.

    They are never used.
    """

    a = secrets.randbelow(9000) + 1000
    b = secrets.randbelow(9000) + 1000

    x = random_name("_j")
    y = random_name("_j")

    junk = (
        f"local {x}={a};"
        f"local {y}={b};"
    )

    return junk + source


# ============================================================
# OBFUSCATION MODES
# ============================================================

def obfuscate(source, mode="MAX"):

    if not source.strip():
        raise ValueError("Пустой файл")

    # --------------------------------------------------------
    # Protect shebang / special first line
    # --------------------------------------------------------

    shebang = ""

    if source.startswith("#!"):
        p = source.find("\n")

        if p != -1:
            shebang = source[:p + 1]
            source = source[p + 1:]
        else:
            shebang = source
            source = ""

    # --------------------------------------------------------
    # Tokenize
    # --------------------------------------------------------

    tokens = tokenize_luau(source)

    # --------------------------------------------------------
    # Remove comments
    # --------------------------------------------------------

    tokens = remove_comments(tokens)

    # --------------------------------------------------------
    # Modes
    # --------------------------------------------------------

    if mode == "SAFE":

        tokens = obfuscate_strings(tokens)

    elif mode == "STRONG":

        tokens = obfuscate_strings(tokens)
        tokens = obfuscate_numbers(tokens)

    elif mode == "MAX":

        tokens = obfuscate_strings(tokens)
        tokens = obfuscate_numbers(tokens)
        tokens = rename_simple_locals(tokens)

    else:
        tokens = obfuscate_strings(tokens)
        tokens = obfuscate_numbers(tokens)
        tokens = rename_simple_locals(tokens)

    # --------------------------------------------------------
    # Generate
    # --------------------------------------------------------

    result = minify_tokens(tokens)

    # --------------------------------------------------------
    # Safe junk only for MAX
    # --------------------------------------------------------

    if mode == "MAX":
        result = add_safe_junk(result)

    return shebang + result


# ============================================================
# TELEGRAM UI
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 SAFE", callback_data="mode_SAFE"),
            InlineKeyboardButton("🟡 STRONG", callback_data="mode_STRONG"),
        ],
        [
            InlineKeyboardButton("🔴 MAX", callback_data="mode_MAX"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    USER_MODE[user_id] = "MAX"

    text = (
        "🔐 <b>Luau Obfuscator</b>\n\n"
        "Загрузи сюда файл <code>.lua</code>.\n\n"
        "<b>Режимы:</b>\n"
        "🟢 SAFE — минимальные изменения\n"
        "🟡 STRONG — строки + числа\n"
        "🔴 MAX — максимальная консервативная обфускация\n\n"
        "⚠️ VM/loadstring намеренно не используются: "
        "главная цель — сохранить совместимость с Luau."
    )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    mode = query.data.replace("mode_", "")

    if mode not in ("SAFE", "STRONG", "MAX"):
        return

    USER_MODE[user_id] = mode

    descriptions = {
        "SAFE": "🟢 SAFE выбран",
        "STRONG": "🟡 STRONG выбран",
        "MAX": "🔴 MAX выбран",
    }

    await query.edit_message_text(
        descriptions[mode]
        + "\n\nТеперь отправь файл `.lua`."
    )


# ============================================================
# FILE PROCESSING
# ============================================================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):

    document = update.message.document

    if not document:
        return

    filename = document.file_name or "script.lua"

    if not filename.lower().endswith(".lua"):
        await update.message.reply_text(
            "❌ Нужен файл с расширением `.lua`."
        )
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            "❌ Файл слишком большой. Максимум 5 MB."
        )
        return

    user_id = update.effective_user.id

    mode = USER_MODE.get(user_id, "MAX")

    status = await update.message.reply_text(
        f"⏳ Обфускация...\nРежим: {mode}"
    )

    try:

        telegram_file = await document.get_file()

        buffer = io.BytesIO()

        await telegram_file.download_to_memory(buffer)

        buffer.seek(0)

        raw = buffer.read()

        # ----------------------------------------------------
        # Decode source
        # ----------------------------------------------------

        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            source = raw.decode("utf-8", errors="replace")

        # ----------------------------------------------------
        # Obfuscate
        # ----------------------------------------------------

        loop = asyncio.get_running_loop()

        output = await loop.run_in_executor(
            None,
            lambda: obfuscate(source, mode),
        )

        # ----------------------------------------------------
        # Prepare file
        # ----------------------------------------------------

        out = io.BytesIO(output.encode("utf-8"))

        out.name = (
            os.path.splitext(filename)[0]
            + "_obf.lua"
        )

        out.seek(0)

        await status.delete()

        await update.message.reply_document(
            document=out,
            caption=(
                "✅ <b>Готово</b>\n\n"
                f"🔐 Режим: <b>{mode}</b>\n"
                f"📄 Файл: <code>{out.name}</code>\n\n"
                "Обфускация сделана без VM/loadstring."
            ),
            parse_mode="HTML",
        )

    except Exception as e:

        try:
            await status.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Ошибка при обработке файла:\n"
            f"<code>{str(e)[:1500]}</code>",
            parse_mode="HTML",
        )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "📄 Отправь именно файл `.lua`.\n\n"
        "Перед этим можешь выбрать режим:",
        reply_markup=main_keyboard(),
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):

    print("BOT ERROR:", repr(context.error))


# ============================================================
# BOT
# ============================================================

def run_bot():

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CallbackQueryHandler(
            mode_callback,
            pattern=r"^mode_"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(error_handler)

    print("Bot started")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    run_bot()
