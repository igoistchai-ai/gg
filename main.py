import os
import re
import ast
import base64
import random
import string
import tempfile
import threading
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

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
    raise RuntimeError("BOT_TOKEN is not configured")

PORT = int(os.getenv("PORT", "10000"))

app = FastAPI()

# user_id -> uploaded Lua source
PENDING = {}

# ============================================================
# HEALTH SERVER
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Lua Obfuscator Bot"
    }


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok"
    })


def run_web():
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )


# ============================================================
# RANDOM UTILITIES
# ============================================================

def rnd_name(length=12):
    alphabet = string.ascii_letters
    return "_" + "".join(random.choice(alphabet) for _ in range(length))


def rnd_key():
    return random.randint(15, 240)


def lua_quote(s):
    return '"' + (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\r", "\\r")
         .replace("\n", "\\n")
         .replace("\t", "\\t")
    ) + '"'


# ============================================================
# LUA STRING LEXER
# ============================================================

def scan_strings(source):
    """
    Находит обычные Lua-строки.
    Не пытается менять содержимое внутри комментариев.
    """

    result = []
    i = 0
    n = len(source)

    while i < n:
        c = source[i]

        # single / double quote
        if c == '"' or c == "'":
            quote = c
            start = i
            i += 1

            escaped = False

            while i < n:
                ch = source[i]

                if escaped:
                    escaped = False
                    i += 1
                    continue

                if ch == "\\":
                    escaped = True
                    i += 1
                    continue

                if ch == quote:
                    i += 1
                    break

                i += 1

            result.append(
                (
                    start,
                    i,
                    source[start:i]
                )
            )

            continue

        # Lua long string
        if source.startswith("[[", i):
            start = i
            end = source.find("]]", i + 2)

            if end != -1:
                end += 2
                result.append(
                    (
                        start,
                        end,
                        source[start:end]
                    )
                )
                i = end
                continue

        # comment
        if source.startswith("--", i):
            # long comment
            if source.startswith("--[[", i):
                end = source.find("]]", i + 4)

                if end == -1:
                    break

                i = end + 2
                continue

            # normal comment
            end = source.find("\n", i + 2)

            if end == -1:
                break

            i = end
            continue

        i += 1

    return result


# ============================================================
# DECODE LUA STRING
# ============================================================

def decode_lua_string(token):
    if len(token) < 2:
        return None

    if token[0] not in "\"'":
        return None

    try:
        # Lua escapes mostly overlap with Python here.
        return ast.literal_eval(token)
    except Exception:
        return None


# ============================================================
# STRING ENCRYPTION
# ============================================================

def xor_bytes(data, key):
    return bytes(
        b ^ key
        for b in data
    )


def make_string_runtime():
    """
    Генерирует Lua runtime для декодирования строк.
    """

    a = rnd_name()
    b = rnd_name()
    c = rnd_name()
    d = rnd_name()
    e = rnd_name()

    runtime = f"""
local {a} = string.char
local {b} = table.concat

local function {c}({d},{e})
    local _r = {{}}
    for _i = 1, #{d} do
        _r[_i] = {a}({d}[_i] ~ {e})
    end
    return {b}(_r)
end
"""

    return runtime, c


def encrypt_strings(source):
    """
    Заменяет строки на вызовы декодера.
    """

    locations = scan_strings(source)

    if not locations:
        return source

    runtime, decoder = make_string_runtime()

    replacements = []

    for start, end, token in locations:
        value = decode_lua_string(token)

        if value is None:
            continue

        # Не трогаем пустые строки
        if value == "":
            continue

        key = rnd_key()

        encoded = [
            b ^ key
            for b in value.encode("utf-8")
        ]

        table = ",".join(str(x) for x in encoded)

        replacement = f"{decoder}({{{table}}},{key})"

        replacements.append(
            (
                start,
                end,
                replacement
            )
        )

    if not replacements:
        return source

    out = source

    for start, end, replacement in reversed(replacements):
        out = (
            out[:start]
            + replacement
            + out[end:]
        )

    return runtime + "\n" + out


# ============================================================
# NUMBER OBFUSCATION
# ============================================================

NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(0[xX][0-9a-fA-F]+|"
    r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"(?![\w.])"
)


def obfuscate_numbers(source):
    """
    123 -> (100 + 23)
    500 -> (700 - 200)

    Только для простых десятичных чисел.
    """

    locations = scan_strings(source)

    protected = []

    for a, b, _ in locations:
        protected.append((a, b))

    def inside_protected(pos):
        for a, b in protected:
            if a <= pos < b:
                return True
        return False

    matches = list(NUMBER_RE.finditer(source))

    replacements = []

    for m in matches:
        if inside_protected(m.start()):
            continue

        token = m.group(1)

        # hex не меняем
        if token.lower().startswith("0x"):
            continue

        # Не трогаем слишком большие/сложные числа
        try:
            value = float(token)

            if not value.is_integer():
                continue

            value = int(value)

        except Exception:
            continue

        # маленькие числа оставляем
        if abs(value) <= 2:
            continue

        mode = random.randint(0, 2)

        if mode == 0:
            a = random.randint(1, max(1, abs(value)))
            b = value - a

            replacement = f"({a}+({b}))"

        elif mode == 1:
            a = value + random.randint(1, 100)
            b = a - value

            replacement = f"({a}-({b}))"

        else:
            k = random.randint(2, 9)
            a = value * k

            replacement = f"(({a})/{k})"

        replacements.append(
            (
                m.start(),
                m.end(),
                replacement
            )
        )

    out = source

    for start, end, replacement in reversed(replacements):
        out = (
            out[:start]
            + replacement
            + out[end:]
        )

    return out


# ============================================================
# IDENTIFIER RENAMING
# ============================================================

LUA_KEYWORDS = {
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
}

PROTECTED_GLOBALS = {
    "print",
    "pairs",
    "ipairs",
    "next",
    "type",
    "tostring",
    "tonumber",
    "select",
    "assert",
    "error",
    "pcall",
    "xpcall",
    "require",
    "load",
    "loadstring",
    "string",
    "table",
    "math",
    "os",
    "io",
    "coroutine",
    "debug",
    "utf8",
    "bit32",
    "_G",
    "_VERSION",
    "self",
}


IDENT_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\b"
)


def collect_local_names(source):
    names = set()

    # local x
    for m in re.finditer(
        r"\blocal\s+([A-Za-z_][A-Za-z0-9_]*)",
        source
    ):
        names.add(m.group(1))

    # local a,b,c
    for m in re.finditer(
        r"\blocal\s+(.+?)(?:=|\n|;)",
        source
    ):
        block = m.group(1)

        for x in re.findall(
            r"[A-Za-z_][A-Za-z0-9_]*",
            block
        ):
            names.add(x)

    # function foo(...)
    for m in re.finditer(
        r"\bfunction\s+[A-Za-z_][A-Za-z0-9_]*\s*\((.*?)\)",
        source,
        re.S
    ):
        args = m.group(1)

        for x in re.findall(
            r"[A-Za-z_][A-Za-z0-9_]*",
            args
        ):
            names.add(x)

    # anonymous function arguments
    for m in re.finditer(
        r"\bfunction\s*\((.*?)\)",
        source,
        re.S
    ):
        args = m.group(1)

        for x in re.findall(
            r"[A-Za-z_][A-Za-z0-9_]*",
            args
        ):
            names.add(x)

    return names


def rename_identifiers(source):
    names = collect_local_names(source)

    mapping = {}

    for name in names:
        if name in LUA_KEYWORDS:
            continue

        if name in PROTECTED_GLOBALS:
            continue

        if len(name) < 1:
            continue

        mapping[name] = rnd_name(
            random.randint(8, 18)
        )

    if not mapping:
        return source

    # защищаем строки
    strings = scan_strings(source)

    def protected(pos):
        for a, b, _ in strings:
            if a <= pos < b:
                return True
        return False

    matches = list(IDENT_RE.finditer(source))

    replacements = []

    for m in matches:
        old = m.group(0)

        if old not in mapping:
            continue

        if protected(m.start()):
            continue

        replacements.append(
            (
                m.start(),
                m.end(),
                mapping[old]
            )
        )

    out = source

    for a, b, replacement in reversed(replacements):
        out = (
            out[:a]
            + replacement
            + out[b:]
        )

    return out


# ============================================================
# COMMENT STRIPPING
# ============================================================

def strip_comments(source):
    out = []
    i = 0
    n = len(source)

    while i < n:
        # strings
        if source[i] in "\"'":
            q = source[i]
            start = i
            i += 1
            escaped = False

            while i < n:
                c = source[i]

                if escaped:
                    escaped = False
                    i += 1
                    continue

                if c == "\\":
                    escaped = True
                    i += 1
                    continue

                if c == q:
                    i += 1
                    break

                i += 1

            out.append(source[start:i])
            continue

        # comment
        if source.startswith("--", i):
            if source.startswith("--[[", i):
                end = source.find("]]", i + 4)

                if end == -1:
                    break

                i = end + 2
                out.append("\n")
                continue

            end = source.find("\n", i + 2)

            if end == -1:
                break

            i = end
            out.append("\n")
            continue

        out.append(source[i])
        i += 1

    return "".join(out)


# ============================================================
# WHITESPACE MINIFICATION
# ============================================================

def minify(source):
    lines = source.splitlines()

    result = []

    for line in lines:
        x = line.strip()

        if not x:
            continue

        result.append(x)

    return "\n".join(result)


# ============================================================
# JUNK CODE
# ============================================================

def junk_block():
    a = rnd_name(10)
    b = rnd_name(10)
    c = random.randint(100, 999999)

    return f"""
do
    local {a} = {c}
    local {b} = ({a} * 3) - ({a} * 3)
    if {b} ~= 0 then
        {a} = {a} + {b}
    end
end
"""


def add_junk(source, amount=3):
    blocks = []

    for _ in range(amount):
        blocks.append(
            junk_block()
        )

    # вставляем сверху
    return "\n".join(blocks) + "\n" + source


# ============================================================
# HEADER
# ============================================================

def obfuscation_header(level):
    tag = "".join(
        random.choice(
            string.ascii_letters + string.digits
        )
        for _ in range(24)
    )

    return f"""--[[

    Lua Obfuscator
    Level: {level}
    Build: {tag}

    Generated automatically.

]]--

"""


# ============================================================
# VM-LIKE LAYER
# ============================================================

def vm_wrap(source):
    """
    Безопасный VM-подобный слой:
    исходный код хранится в закодированном виде,
    затем runtime декодирует его и передаёт load().
    
    Это НЕ полноценная виртуализация Lua bytecode.
    """

    key = random.randint(1, 255)

    encoded = xor_bytes(
        source.encode("utf-8"),
        key
    )

    values = ",".join(
        str(x)
        for x in encoded
    )

    a = rnd_name(14)
    b = rnd_name(14)
    c = rnd_name(14)
    d = rnd_name(14)
    e = rnd_name(14)
    f = rnd_name(14)

    vm = f"""
local {a}={key}
local {b}={{{values}}}

local function {c}({d})
    local {e}={{}}
    for {f}=1,#{d} do
        {e}[{f}]=string.char({d}[{f}] ~ {a})
    end
    return table.concat({e})
end

local _chunk = {c}({b})
local _fn, _err = load(_chunk)

if not _fn then
    error(_err)
end

return _fn()
"""

    return vm


# ============================================================
# EXTREME WRAPPER
# ============================================================

def extreme_wrap(source):
    """
    Дополнительный runtime-слой.
    """

    key1 = random.randint(20, 230)
    key2 = random.randint(20, 230)

    encoded = []

    for b in source.encode("utf-8"):
        x = b ^ key1
        x = (x + key2) % 256
        encoded.append(x)

    values = ",".join(
        str(x)
        for x in encoded
    )

    a = rnd_name(15)
    b = rnd_name(15)
    c = rnd_name(15)
    d = rnd_name(15)
    e = rnd_name(15)

    return f"""
local {a}={key1}
local {b}={key2}
local {c}={{{values}}}

local function {d}(_x)
    local _r={{}}

    for {e}=1,#{_x} do
        _r[{e}]=string.char(
            (((_x[{e}]-{b})%256)~{a})
        )
    end

    return table.concat(_r)
end

local _source={d}({c})
local _loader,_error=load(_source)

if not _loader then
    error(_error)
end

return _loader()
"""


# ============================================================
# MAIN OBFUSCATOR
# ============================================================

def obfuscate(source, level):
    original = source

    # 1. remove comments
    source = strip_comments(source)

    # 2. strings
    if level in ("strong", "extreme", "vm"):
        source = encrypt_strings(source)

    # 3. numbers
    if level in ("strong", "extreme", "vm"):
        source = obfuscate_numbers(source)

    # 4. rename
    if level in ("strong", "extreme", "vm"):
        source = rename_identifiers(source)

    # 5. junk
    if level == "extreme":
        source = add_junk(
            source,
            random.randint(3, 7)
        )

    # 6. minify
    if level in ("strong", "extreme"):
        source = minify(source)

    # 7. VM-like packaging
    if level == "vm":
        # Сначала готовим внутренний код,
        # затем кодируем его целиком.
        source = vm_wrap(source)

    # 8. additional layer
    if level == "extreme":
        source = extreme_wrap(source)

    header = obfuscation_header(level)

    result = header + source

    # если обфускация почему-то стала пустой
    if len(result.strip()) < 30:
        return original

    return result


# ============================================================
# TELEGRAM UI
# ============================================================

def levels_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🟢 Basic",
                callback_data="obf:basic"
            ),
            InlineKeyboardButton(
                "🟡 Strong",
                callback_data="obf:strong"
            ),
        ],
        [
            InlineKeyboardButton(
                "🔴 Extreme",
                callback_data="obf:extreme"
            ),
            InlineKeyboardButton(
                "💀 VM",
                callback_data="obf:vm"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ Отмена",
                callback_data="obf:cancel"
            )
        ]
    ])


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 Lua Obfuscator\n\n"
        "Отправь мне файл .lua.\n\n"
        "После загрузки можно выбрать уровень:\n\n"
        "🟢 Basic — базовое скрытие\n"
        "🟡 Strong — строки + числа + имена\n"
        "🔴 Extreme — дополнительные слои\n"
        "💀 VM — упаковка через runtime\n\n"
        "Файл после обработки будет отправлен обратно."
    )


# ============================================================
# DOCUMENT RECEIVER
# ============================================================

async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    document = update.message.document

    if not document:
        return

    filename = document.file_name or "input.lua"

    if not filename.lower().endswith(".lua"):
        await update.message.reply_text(
            "❌ Нужен файл с расширением .lua"
        )
        return

    if document.file_size and document.file_size > 2 * 1024 * 1024:
        await update.message.reply_text(
            "❌ Максимальный размер файла: 2 MB."
        )
        return

    user_id = update.effective_user.id

    status = await update.message.reply_text(
        "📥 Загружаю Lua-файл..."
    )

    try:
        tg_file = await context.bot.get_file(
            document.file_id
        )

        data = await tg_file.download_as_bytearray()

        source = bytes(data).decode(
            "utf-8",
            errors="replace"
        )

        if not source.strip():
            await status.edit_text(
                "❌ Файл пустой."
            )
            return

        PENDING[user_id] = {
            "filename": filename,
            "source": source
        }

        await status.edit_text(
            f"📄 Файл: `{filename}`\n"
            f"📦 Размер: {len(data):,} байт\n\n"
            "Выбери режим:",
            parse_mode="Markdown",
            reply_markup=levels_keyboard()
        )

    except Exception as e:
        await status.edit_text(
            f"❌ Ошибка загрузки:\n`{str(e)[:1000]}`",
            parse_mode="Markdown"
        )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if query.data == "obf:cancel":
        PENDING.pop(user_id, None)

        await query.edit_message_text(
            "❌ Обработка отменена."
        )

        return

    if not query.data.startswith("obf:"):
        return

    level = query.data.split(":", 1)[1]

    if user_id not in PENDING:
        await query.edit_message_text(
            "❌ Файл не найден.\n"
            "Отправь .lua заново."
        )
        return

    item = PENDING[user_id]

    source = item["source"]
    filename = item["filename"]

    level_names = {
        "basic": "Basic",
        "strong": "Strong",
        "extreme": "Extreme",
        "vm": "VM"
    }

    await query.edit_message_text(
        f"⚙️ Обрабатываю...\n\n"
        f"Режим: {level_names.get(level, level)}"
    )

    try:
        loop = asyncio.get_running_loop()

        result = await loop.run_in_executor(
            None,
            obfuscate,
            source,
            level
        )

        # проверяем, что результат вообще существует
        if not result.strip():
            raise RuntimeError(
                "Обфускатор вернул пустой результат"
            )

        output_name = (
            Path(filename).stem
            + "_obfuscated.lua"
        )

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lua",
            delete=False,
            encoding="utf-8"
        ) as f:
            f.write(result)
            temp_path = f.name

        original_size = len(
            source.encode("utf-8")
        )

        result_size = len(
            result.encode("utf-8")
        )

        await query.edit_message_text(
            "✅ Обфускация завершена.\n\n"
            f"Режим: {level_names.get(level, level)}\n"
            f"Исходник: {original_size:,} байт\n"
            f"Результат: {result_size:,} байт"
        )

        with open(temp_path, "rb") as f:
            await context.bot.send_document(
                chat_id=user_id,
                document=f,
                filename=output_name,
                caption=(
                    "🔐 Готово!\n\n"
                    f"Режим: {level_names.get(level, level)}\n"
                    f"Файл: {output_name}"
                )
            )

        try:
            os.remove(temp_path)
        except Exception:
            pass

        PENDING.pop(user_id, None)

    except Exception as e:
        await query.edit_message_text(
            "❌ Ошибка обфускации:\n\n"
            f"{str(e)[:2000]}"
        )

        PENDING.pop(user_id, None)


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "📁 Отправь Lua-файл документом.\n\n"
        "Например:\n"
        "`script.lua`",
        parse_mode="Markdown"
    )


# ============================================================
# BOT STARTUP
# ============================================================

def create_bot():
    bot = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    bot.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    bot.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler
        )
    )

    bot.add_handler(
        CallbackQueryHandler(
            button_handler,
            pattern=r"^obf:"
        )
    )

    bot.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    return bot


# ============================================================
# MAIN
# ============================================================

def main():
    # HTTP-сервер нужен Render/UptimeRobot
    thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    thread.start()

    bot = create_bot()

    print("================================")
    print("Lua Obfuscator Bot")
    print("HTTP server: ONLINE")
    print("Telegram bot: STARTING")
    print("================================")

    bot.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
