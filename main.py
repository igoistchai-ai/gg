import os
import io
import time
import asyncio
import secrets
import string
import threading
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, Response
import uvicorn

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

PORT = int(os.getenv("PORT", "10000"))

PUBLIC_URL = (
    os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
)

MAX_FILE_SIZE = 5 * 1024 * 1024

# Время жизни ссылки.
# 0 = бессрочно.
LINK_TTL = int(os.getenv("LINK_TTL", "86400"))

# ============================================================
# PRIVATE STORAGE
# ============================================================

STORAGE = Path("private_storage")
STORAGE.mkdir(parents=True, exist_ok=True)

META_STORAGE = STORAGE / "_meta"
META_STORAGE.mkdir(parents=True, exist_ok=True)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ============================================================
# 404
# ============================================================

def response_404():
    return PlainTextResponse(
        "404 Not Found",
        status_code=404,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.exception_handler(404)
async def exception_404(request, exc):
    return response_404()


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {"status": "ok"}


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():
    return response_404()


# ============================================================
# TOKEN
# ============================================================

TOKEN_ALPHABET = (
    string.ascii_letters +
    string.digits
)


def create_token(length=12):

    while True:

        token = "".join(
            secrets.choice(TOKEN_ALPHABET)
            for _ in range(length)
        )

        source_path = STORAGE / f"{token}.lua"
        meta_path = META_STORAGE / f"{token}.json"

        if not source_path.exists() and not meta_path.exists():
            return token


# ============================================================
# METADATA
# ============================================================

def save_metadata(token, telegram_id, filename):

    data = {
        "token": token,
        "telegram_id": int(telegram_id),
        "filename": filename,
        "created_at": int(time.time()),
    }

    path = META_STORAGE / f"{token}.json"

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False
        ),
        encoding="utf-8",
    )


def load_metadata(token):

    path = META_STORAGE / f"{token}.json"

    if not path.is_file():
        return None

    try:

        return json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return None


def delete_metadata(token):

    path = META_STORAGE / f"{token}.json"

    try:

        if path.exists():
            path.unlink()

    except Exception:
        pass


# ============================================================
# LUA LEXER
# ============================================================

IDENT_START = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "_"
)

IDENT_BODY = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_"
)

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


def long_bracket_end(source, start):

    if start >= len(source):
        return None

    if source[start] != "[":
        return None

    i = start + 1
    level = 0

    while i < len(source) and source[i] == "=":
        level += 1
        i += 1

    if i >= len(source):
        return None

    if source[i] != "[":
        return None

    close = "]" + ("=" * level) + "]"

    pos = source.find(
        close,
        i + 1
    )

    if pos == -1:
        return len(source)

    return pos + len(close)


def read_quoted_string(source, start):

    quote = source[start]

    i = start + 1

    while i < len(source):

        c = source[i]

        if c == "\\":
            i += 2
            continue

        if c == quote:
            return i + 1

        i += 1

    return len(source)


def tokenize(source):

    tokens = []

    i = 0
    n = len(source)

    while i < n:

        c = source[i]

        # ----------------------------------------------------
        # whitespace
        # ----------------------------------------------------

        if c.isspace():

            i += 1
            continue

        # ----------------------------------------------------
        # comments
        # ----------------------------------------------------

        if (
            c == "-"
            and i + 1 < n
            and source[i + 1] == "-"
        ):

            end = long_bracket_end(
                source,
                i + 2
            )

            if end is not None:

                i = end
                continue

            i += 2

            while (
                i < n
                and source[i] not in "\r\n"
            ):
                i += 1

            continue

        # ----------------------------------------------------
        # quoted strings
        # ----------------------------------------------------

        if c == "'" or c == '"':

            end = read_quoted_string(
                source,
                i
            )

            tokens.append(
                ("string", source[i:end])
            )

            i = end
            continue

        # ----------------------------------------------------
        # long strings
        # ----------------------------------------------------

        if c == "[":

            end = long_bracket_end(
                source,
                i
            )

            if end is not None:

                tokens.append(
                    ("string", source[i:end])
                )

                i = end
                continue

        # ----------------------------------------------------
        # identifiers
        # ----------------------------------------------------

        if c in IDENT_START:

            j = i + 1

            while (
                j < n
                and source[j] in IDENT_BODY
            ):
                j += 1

            value = source[i:j]

            if value in KEYWORDS:

                tokens.append(
                    ("keyword", value)
                )

            else:

                tokens.append(
                    ("identifier", value)
                )

            i = j
            continue

        # ----------------------------------------------------
        # numbers
        # ----------------------------------------------------

        if c.isdigit():

            j = i + 1

            while (
                j < n
                and (
                    source[j].isalnum()
                    or source[j] in "._"
                )
            ):
                j += 1

            tokens.append(
                ("number", source[i:j])
            )

            i = j
            continue

        # ----------------------------------------------------
        # operators
        # ----------------------------------------------------

        found = False

        for op in (
            "...",
            "::",
            "==",
            "~=",
            "<=",
            ">=",
            "..",
            "+=",
            "-=",
            "*=",
            "/=",
            "%=",
            "^=",
            "//",
            "->",
        ):

            if source.startswith(
                op,
                i
            ):

                tokens.append(
                    ("symbol", op)
                )

                i += len(op)
                found = True

                break

        if found:
            continue

        # ----------------------------------------------------
        # single character
        # ----------------------------------------------------

        tokens.append(
            ("symbol", c)
        )

        i += 1

    return tokens


# ============================================================
# STRING OBFUSCATION
# ============================================================

def lua_string_expression(raw):

    if len(raw) < 2:
        return raw

    if raw[0] not in ("'", '"'):
        return raw

    quote = raw[0]

    if raw[-1] != quote:
        return raw

    content = raw[1:-1]

    chars = []

    i = 0

    while i < len(content):

        c = content[i]

        if c != "\\":

            chars.append(
                ord(c)
            )

            i += 1
            continue

        if i + 1 >= len(content):
            return raw

        nxt = content[i + 1]

        escapes = {
            "n": 10,
            "r": 13,
            "t": 9,
            "b": 8,
            "f": 12,
            "v": 11,
            "\\": 92,
            "'": 39,
            '"': 34,
            "0": 0,
        }

        if nxt in escapes:

            chars.append(
                escapes[nxt]
            )

            i += 2
            continue

        # Numeric escape
        if nxt.isdigit():

            j = i + 1

            while (
                j < len(content)
                and j < i + 4
                and content[j].isdigit()
            ):
                j += 1

            try:

                number = int(
                    content[i + 1:j]
                )

                if 0 <= number <= 255:

                    chars.append(
                        number
                    )

                    i = j
                    continue

            except Exception:
                pass

            return raw

        return raw

    if not chars:
        return '""'

    # Перемешиваем аргументы string.char.
    pairs = list(enumerate(chars))

    if len(pairs) > 3:

        secrets.SystemRandom().shuffle(
            pairs
        )

    values = [
        str(value)
        for _, value in pairs
    ]

    return (
        "string.char("
        + ",".join(values)
        + ")"
    )


# ============================================================
# LOCAL RENAMING
# ============================================================

LUA_RESERVED = KEYWORDS | {
    "self",
    "_ENV",
    "_G",
}


def generate_name(index):

    alphabet = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )

    if index < len(alphabet):

        return "_" + alphabet[index]

    return (
        "_v"
        + format(index, "x")
    )


def collect_safe_locals(tokens):

    names = []

    i = 0

    while i < len(tokens):

        kind, value = tokens[i]

        if (
            kind == "keyword"
            and value == "local"
        ):

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

                    if (
                        name not in LUA_RESERVED
                        and name not in names
                    ):

                        names.append(name)

                i = j
                continue

            # local a,b,c
            while j < len(tokens):

                k, v = tokens[j]

                if k != "identifier":
                    break

                if (
                    v not in LUA_RESERVED
                    and v not in names
                ):

                    names.append(v)

                j += 1

                if (
                    j < len(tokens)
                    and tokens[j][1] == ","
                ):

                    j += 1
                    continue

                break

        i += 1

    return names


def rename_locals(tokens):

    names = collect_safe_locals(
        tokens
    )

    if not names:
        return tokens

    mapping = {}

    index = 0

    for name in names:

        new_name = generate_name(
            index
        )

        index += 1

        if new_name != name:

            mapping[name] = new_name

    result = []

    for i, (kind, value) in enumerate(tokens):

        if kind != "identifier":

            result.append(
                (kind, value)
            )

            continue

        previous = (
            tokens[i - 1][1]
            if i > 0
            else ""
        )

        # table.field
        if previous == ".":

            result.append(
                (kind, value)
            )

            continue

        # object:method
        if previous == ":":

            result.append(
                (kind, value)
            )

            continue

        if value in mapping:

            result.append(
                (
                    "identifier",
                    mapping[value]
                )
            )

        else:

            result.append(
                (kind, value)
            )

    return result


# ============================================================
# MINIFY
# ============================================================

def needs_space(previous, current):

    if not previous:
        return False

    pk, pv = previous
    ck, cv = current

    if (
        pk in (
            "identifier",
            "keyword",
            "number"
        )
        and
        ck in (
            "identifier",
            "keyword",
            "number"
        )
    ):
        return True

    if (
        pk in (
            "keyword",
            "identifier"
        )
        and ck == "string"
    ):
        return True

    if (
        pk == "number"
        and ck == "identifier"
    ):
        return True

    return False


def minify_tokens(tokens):

    result = []

    previous = None

    for token in tokens:

        if needs_space(
            previous,
            token
        ):
            result.append(" ")

        result.append(
            token[1]
        )

        previous = token

    return "".join(result)


# ============================================================
# OBFUSCATOR
# ============================================================

def obfuscate(source):

    tokens = tokenize(source)

    transformed = []

    for kind, value in tokens:

        if kind == "string":

            transformed.append(
                (
                    "expression",
                    lua_string_expression(
                        value
                    )
                )
            )

        else:

            transformed.append(
                (
                    kind,
                    value
                )
            )

    transformed = rename_locals(
        transformed
    )

    result = minify_tokens(
        transformed
    )

    # Безопасная дополнительная
    # переменная-мусор.
    junk_name = (
        "_obf_"
        + secrets.token_hex(5)
    )

    junk_value = (
        secrets.randbelow(900000)
        + 100000
    )

    result = (
        f"local {junk_name}={junk_value};"
        + result
    )

    return result


# ============================================================
# SAVE OBFUSCATED SOURCE
# ============================================================

def save_obfuscated_source(
    source,
    telegram_id,
    filename
):

    if len(
        source.encode("utf-8")
    ) > MAX_FILE_SIZE:

        raise ValueError(
            "File too large"
        )

    # ========================================================
    # ORIGINAL -> OBFUSCATED
    # ========================================================

    obfuscated = obfuscate(
        source
    )

    token = create_token(
        12
    )

    source_path = (
        STORAGE
        / f"{token}.lua"
    )

    # В storage записывается
    # ТОЛЬКО обфусцированный код.
    source_path.write_text(
        obfuscated,
        encoding="utf-8",
    )

    # Информация о владельце
    save_metadata(
        token,
        telegram_id,
        filename
    )

    return token


# ============================================================
# DELETE LINK
# ============================================================

def delete_link(token):

    source_path = (
        STORAGE
        / f"{token}.lua"
    )

    if not source_path.exists():
        return False

    try:
        source_path.unlink()
    except Exception:
        return False

    delete_metadata(
        token
    )

    return True


# ============================================================
# LOADSTRING
# ============================================================

def make_loadstring(url):

    safe_url = (
        url
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )

    return (
        'loadstring(game:HttpGet("'
        + safe_url
        + '"))()'
    )


# ============================================================
# SERVE SCRIPT
# ============================================================

@app.get("/x/{token}")
async def serve_script(token: str):

    if len(token) != 12:
        return response_404()

    if not all(
        c in TOKEN_ALPHABET
        for c in token
    ):
        return response_404()

    path = (
        STORAGE
        / f"{token}.lua"
    )

    if not path.is_file():
        return response_404()

    # ========================================================
    # TTL
    # ========================================================

    if LINK_TTL > 0:

        try:

            age = (
                time.time()
                - path.stat().st_mtime
            )

            if age > LINK_TTL:

                delete_link(
                    token
                )

                return response_404()

        except Exception:

            return response_404()

    try:

        source = path.read_text(
            encoding="utf-8"
        )

    except Exception:

        return response_404()

    return Response(
        content=source,
        media_type=(
            "text/plain; charset=utf-8"
        ),
        headers={
            "Cache-Control": (
                "no-store,"
                "no-cache,"
                "must-revalidate,"
                "max-age=0"
            ),
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ============================================================
# CATCH ALL -> 404
# ============================================================

@app.api_route(
    "/{path:path}",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "HEAD",
    ],
)
async def catch_all(path: str):

    return response_404()


# ============================================================
# CLEANUP
# ============================================================

def cleanup_old_files():

    if LINK_TTL <= 0:
        return

    now = time.time()

    for path in STORAGE.glob("*.lua"):

        try:

            age = (
                now
                - path.stat().st_mtime
            )

            if age > LINK_TTL:

                token = path.stem

                delete_link(
                    token
                )

        except Exception:
            pass


def cleanup_worker():

    while True:

        try:
            cleanup_old_files()

        except Exception as e:

            print(
                "CLEANUP ERROR:",
                repr(e)
            )

        time.sleep(
            3600
        )


# ============================================================
# TELEGRAM START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "👋 Отправь .lua файл.\n\n"
        "Я автоматически обфусцирую его "
        "и создам короткий loader."
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📁 Отправь файл .lua."
    )


# ============================================================
# LUA PROCESSING
# ============================================================

async def process_lua(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    if (
        not message
        or not message.document
    ):
        return

    document = message.document

    filename = (
        document.file_name
        or "script.lua"
    )

    if not filename.lower().endswith(
        ".lua"
    ):

        await message.reply_text(
            "❌ Нужен файл .lua"
        )

        return

    if (
        document.file_size
        and document.file_size
        > MAX_FILE_SIZE
    ):

        await message.reply_text(
            "❌ Максимальный размер — 5 MB."
        )

        return

    status = await message.reply_text(
        "⏳ Обфусцирую..."
    )

    try:

        telegram_file = (
            await document.get_file()
        )

        buffer = io.BytesIO()

        await telegram_file.download_to_memory(
            buffer
        )

        raw = buffer.getvalue()

        if len(raw) > MAX_FILE_SIZE:

            await status.edit_text(
                "❌ Файл слишком большой."
            )

            return

        try:

            original_source = raw.decode(
                "utf-8"
            )

        except UnicodeDecodeError:

            await status.edit_text(
                "❌ Файл должен быть UTF-8."
            )

            return

        telegram_id = (
            update.effective_user.id
        )

        # ====================================================
        # ORIGINAL
        #    ↓
        # OBFUSCATED
        #    ↓
        # PRIVATE STORAGE
        # ====================================================

        token = await asyncio.to_thread(
            save_obfuscated_source,
            original_source,
            telegram_id,
            filename
        )

        # Оригинал больше не нужен.
        del original_source
        del raw

        if not PUBLIC_URL:

            await status.edit_text(
                "❌ Не найден "
                "RENDER_EXTERNAL_URL."
            )

            return

        url = (
            f"{PUBLIC_URL}/x/{token}"
        )

        command = make_loadstring(
            url
        )

        # ====================================================
        # TTL TEXT
        # ====================================================

        if LINK_TTL <= 0:

            lifetime_text = (
                "♾ Lifetime: бессрочно"
            )

        else:

            hours = LINK_TTL // 3600

            if hours >= 24:

                days = hours // 24

                lifetime_text = (
                    f"⏱ Lifetime: {days} дн."
                )

            else:

                lifetime_text = (
                    f"⏱ Lifetime: {hours} ч."
                )

        # ====================================================
        # DELETE BUTTON
        # ====================================================

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🚨 Экстренно удалить ссылку",
                        callback_data=(
                            f"delete:{token}"
                        ),
                    )
                ]
            ]
        )

        try:
            await status.delete()
        except Exception:
            pass

        # ====================================================
        # MESSAGE
        # ====================================================

        await message.reply_text(
            "✅ Обфускация завершена\n\n"
            "📌 Loadstring:\n\n"
            f"{command}\n\n"
            f"🔗 Token: {token}\n"
            f"{lifetime_text}\n\n"
            "⚠️ После удаления ссылка "
            "сразу перестанет работать.",
            reply_markup=keyboard,
        )

        # ====================================================
        # LOADER FILE
        # ====================================================

        loader = (
            command
            + "\n"
        )

        loader_buffer = io.BytesIO(
            loader.encode("utf-8")
        )

        loader_buffer.name = (
            "loader.lua"
        )

        await message.reply_document(
            document=loader_buffer,
            caption=(
                "📄 loader.lua\n"
                "🚨 Для удаления ссылки "
                "используй кнопку выше."
            ),
        )

    except Exception as e:

        print(
            "PROCESS ERROR:",
            repr(e)
        )

        try:

            await status.edit_text(
                "❌ Ошибка при обфускации."
            )

        except Exception:
            pass


# ============================================================
# DELETE CALLBACK
# ============================================================

async def delete_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data or ""

    if not data.startswith(
        "delete:"
    ):
        return

    token = data[
        len("delete:"):
    ]

    # ========================================================
    # Проверяем token
    # ========================================================

    if len(token) != 12:

        await query.edit_message_text(
            "❌ Недействительная ссылка."
        )

        return

    if not all(
        c in TOKEN_ALPHABET
        for c in token
    ):

        await query.edit_message_text(
            "❌ Недействительная ссылка."
        )

        return

    # ========================================================
    # Проверяем владельца
    # ========================================================

    metadata = load_metadata(
        token
    )

    if not metadata:

        await query.edit_message_text(
            "❌ Ссылка уже удалена "
            "или больше не существует."
        )

        return

    owner_id = metadata.get(
        "telegram_id"
    )

    current_user_id = (
        update.effective_user.id
    )

    if int(owner_id) != int(
        current_user_id
    ):

        await query.answer(
            "⛔ Эта ссылка принадлежит другому пользователю.",
            show_alert=True,
        )

        return

    # ========================================================
    # DELETE
    # ========================================================

    deleted = await asyncio.to_thread(
        delete_link,
        token
    )

    if not deleted:

        await query.edit_message_text(
            "❌ Ссылка уже удалена."
        )

        return

    # ========================================================
    # SUCCESS
    # ========================================================

    await query.edit_message_text(
        "🚨 Ссылка экстренно удалена.\n\n"
        f"Token: {token}\n\n"
        "Сервер больше не выдаёт "
        "этот скрипт. Запрос к ссылке "
        "возвращает 404."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📁 Отправь файл .lua"
    )


# ============================================================
# WEB SERVER
# ============================================================

def run_web():

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="warning",
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("LUA OBFUSCATOR + PRIVATE LOADER")
    print("=" * 60)

    print(
        "PORT:",
        PORT
    )

    print(
        "PUBLIC URL:",
        PUBLIC_URL or "AUTO"
    )

    print(
        "LINK TTL:",
        LINK_TTL
    )

    print(
        "PRIVATE STORAGE:",
        STORAGE.absolute()
    )

    # --------------------------------------------------------
    # Web server
    # --------------------------------------------------------

    threading.Thread(
        target=run_web,
        daemon=True,
    ).start()

    # --------------------------------------------------------
    # Cleanup
    # --------------------------------------------------------

    threading.Thread(
        target=cleanup_worker,
        daemon=True,
    ).start()

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    bot = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    bot.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    bot.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    bot.add_handler(
        CallbackQueryHandler(
            delete_callback,
            pattern=r"^delete:",
        )
    )

    bot.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_lua,
        )
    )

    bot.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    print(
        "Telegram bot started"
    )

    bot.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
