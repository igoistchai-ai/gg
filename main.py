import os
import re
import html
import asyncio
import logging
import tempfile
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

PORT = int(os.getenv("PORT", "10000"))
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("lua-deobfuscator")

# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ============================================================
# HEALTH SERVER FOR RENDER
# ============================================================

async def health(request):
    return web.Response(
        text="Lua Deobfuscator Bot is running.",
        content_type="text/plain"
    )


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT
    )

    await site.start()

    logger.info("HTTP server started on port %s", PORT)

# ============================================================
# LUA ANALYSIS
# ============================================================

def count_lines(code: str) -> int:
    if not code:
        return 0

    return len(code.splitlines())


def analyze_code(code: str) -> dict:
    patterns = {
        "string.char": r"\bstring\s*\.\s*char\s*\(",
        "string.byte": r"\bstring\s*\.\s*byte\s*\(",
        "string.reverse": r"\bstring\s*\.\s*reverse\s*\(",
        "string.rep": r"\bstring\s*\.\s*rep\s*\(",
        "loadstring": r"\bloadstring\s*\(",
        "load": r"\bload\s*\(",
        "table.concat": r"\btable\s*\.\s*concat\s*\(",
        "getfenv": r"\bgetfenv\s*\(",
        "setfenv": r"\bsetfenv\s*\(",
        "escaped strings": r"\\\d{1,3}",
        "hex escapes": r"\\x[0-9a-fA-F]{2}",
    }

    result = {}

    for name, pattern in patterns.items():
        result[name] = len(re.findall(pattern, code))

    suspicious_words = [
        "obfusc",
        "encoded",
        "decode",
        "decrypt",
        "xor",
        "base64",
        "virtual",
        "vm",
    ]

    lower = code.lower()

    result["possible indicators"] = sum(
        lower.count(word)
        for word in suspicious_words
    )

    return result


# ============================================================
# SAFE STRING DECODERS
# ============================================================

def decode_decimal_escapes(value: str) -> str:
    """
    Converts Lua decimal escapes such as:

        "hello\\32world"

    into:

        "hello world"

    Only works inside quoted strings.
    """

    def repl(match):
        number = match.group(1)

        try:
            n = int(number)

            if 0 <= n <= 255:
                return chr(n)

        except Exception:
            pass

        return match.group(0)

    return re.sub(r"\\([0-9]{1,3})", repl, value)


def decode_hex_escapes(value: str) -> str:
    """
    Converts Lua-style hexadecimal escapes:

        \\x41

    into:

        A
    """

    def repl(match):
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)

    return re.sub(
        r"\\x([0-9a-fA-F]{2})",
        repl,
        value
    )


def decode_common_escapes(value: str) -> str:
    """
    Decodes common Lua string escapes without touching
    arbitrary source code.
    """

    replacements = {
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\0": "\0",
        r"\\": "\\",
        r"\"": '"',
        r"\'": "'",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = decode_decimal_escapes(value)
    value = decode_hex_escapes(value)

    return value


def decode_quoted_strings(code: str) -> str:
    """
    Finds normal Lua quoted strings and decodes safe escape
    sequences.

    Does NOT execute Lua.
    """

    pattern = re.compile(
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    )

    def repl(match):
        original = match.group(0)

        quote = original[0]
        content = original[1:-1]

        decoded = decode_common_escapes(content)

        return quote + decoded + quote

    return pattern.sub(repl, code)


# ============================================================
# STRING.CHAR
# ============================================================

def parse_number(value: str):
    value = value.strip()

    try:
        if value.lower().startswith("0x"):
            return int(value, 16)

        return int(value, 10)

    except Exception:
        return None


def decode_string_char(code: str) -> str:
    """
    Converts simple:

        string.char(72,101,108,108,111)

    into:

        "Hello"

    Only numeric arguments are processed.
    """

    pattern = re.compile(
        r"string\s*\.\s*char\s*\(\s*"
        r"([0-9xa-fA-F,\s]+)"
        r"\)"
    )

    def repl(match):
        inside = match.group(1)

        parts = [
            x.strip()
            for x in inside.split(",")
            if x.strip()
        ]

        numbers = []

        for part in parts:
            n = parse_number(part)

            if n is None or not 0 <= n <= 255:
                return match.group(0)

            numbers.append(n)

        if not numbers:
            return match.group(0)

        try:
            decoded = "".join(chr(x) for x in numbers)

            return '"' + decoded.replace('"', '\\"') + '"'

        except Exception:
            return match.group(0)

    return pattern.sub(repl, code)


# ============================================================
# STRING.REVERSE
# ============================================================

def decode_string_reverse(code: str) -> str:
    """
    Converts:

        string.reverse("abc")

    into:

        "cba"

    Only literal strings are processed.
    """

    pattern = re.compile(
        r"string\s*\.\s*reverse\s*\(\s*"
        r'(["\'])(.*?)\1\s*\)'
    )

    def repl(match):
        quote = match.group(1)
        value = match.group(2)

        return quote + value[::-1] + quote

    return pattern.sub(repl, code)


# ============================================================
# SIMPLE STRING CONCATENATION
# ============================================================

def decode_simple_concat(code: str) -> str:
    """
    Converts simple constant concatenations:

        "hel" .. "lo"

    into:

        "hello"

    This intentionally avoids complex expressions.
    """

    pattern = re.compile(
        r'(["\'])([^"\']*)\1'
        r'\s*\.\.\s*'
        r'(["\'])([^"\']*)\3'
    )

    changed = True

    while changed:
        new_code = pattern.sub(
            lambda m:
            '"' + m.group(2) + m.group(4) + '"',
            code
        )

        changed = new_code != code
        code = new_code

    return code


# ============================================================
# COMMENTS / FORMATTING
# ============================================================

def normalize_whitespace(code: str) -> str:
    """
    Light formatting only.
    """

    code = code.replace("\r\n", "\n")
    code = code.replace("\r", "\n")

    lines = []

    for line in code.splitlines():
        line = line.rstrip()

        if line.strip():
            lines.append(line)

    return "\n".join(lines)


def basic_indent(code: str) -> str:
    """
    Lightweight Lua indentation.

    This is intentionally conservative because Lua syntax
    can be dynamically generated.
    """

    lines = code.splitlines()

    result = []
    indent = 0

    decrease_words = (
        "end",
        "until",
        "else",
        "elseif",
    )

    increase_words = (
        "function",
        "then",
        "do",
        "repeat",
    )

    for line in lines:
        stripped = line.strip()

        if not stripped:
            result.append("")
            continue

        lower = stripped.lower()

        if any(
            lower.startswith(word)
            for word in decrease_words
        ):
            indent = max(0, indent - 1)

        result.append(
            "    " * indent + stripped
        )

        # Don't increase after comments
        if stripped.startswith("--"):
            continue

        # elseif / else should restore indentation
        if lower.startswith("else"):
            indent += 1

        elif lower.endswith("then"):
            indent += 1

        elif lower.endswith(" do"):
            indent += 1

        elif lower.startswith("function "):
            indent += 1

    return "\n".join(result)


# ============================================================
# DEOBFUSCATION PIPELINE
# ============================================================

def deobfuscate(code: str):
    original = code

    passes = []

    # Pass 1
    code = decode_quoted_strings(code)

    if code != original:
        passes.append("decoded Lua string escapes")

    # Pass 2
    old = code
    code = decode_string_char(code)

    if code != old:
        passes.append("decoded string.char()")

    # Pass 3
    old = code
    code = decode_string_reverse(code)

    if code != old:
        passes.append("decoded string.reverse()")

    # Pass 4
    old = code
    code = decode_simple_concat(code)

    if code != old:
        passes.append("merged constant string concatenations")

    # Pass 5
    code = normalize_whitespace()

    # Pass 6
    code = basic_indent(code)

    return code, passes


# ============================================================
# HTML GENERATOR
# ============================================================

def escape_code(code: str) -> str:
    return html.escape(code, quote=False)


def create_html(
    original: str,
    result: str,
    analysis: dict,
    passes: list,
    filename: str
) -> str:

    original_lines = count_lines(original)
    result_lines = count_lines(result)

    analysis_html = ""

    for key, value in analysis.items():
        analysis_html += f"""
        <div class="stat">
            <span>{html.escape(str(key))}</span>
            <strong>{html.escape(str(value))}</strong>
        </div>
        """

    passes_html = ""

    if passes:
        for item in passes:
            passes_html += (
                f"<li>{html.escape(item)}</li>"
            )
    else:
        passes_html = (
            "<li>Автоматических преобразований не найдено</li>"
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1.0">

<title>Lua Deobfuscator — {html.escape(filename)}</title>

<style>
* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;
    background: #080b12;
    color: #e8edf7;
    font-family:
        Inter,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}}

.container {{
    width: min(1400px, 94%);
    margin: 30px auto;
}}

.header {{
    padding: 28px;
    border: 1px solid #202838;
    border-radius: 22px;
    background:
        linear-gradient(
            135deg,
            #111827,
            #0b101a
        );
    margin-bottom: 20px;
}}

.header h1 {{
    margin: 0 0 8px;
    font-size: 28px;
}}

.header p {{
    margin: 0;
    color: #8d98aa;
}}

.grid {{
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
}}

.stat {{
    border: 1px solid #202838;
    border-radius: 16px;
    background: #0d121c;
    padding: 17px;
}}

.stat span {{
    display: block;
    color: #8994a8;
    font-size: 13px;
    margin-bottom: 8px;
}}

.stat strong {{
    font-size: 22px;
}}

.panel {{
    border: 1px solid #202838;
    border-radius: 20px;
    background: #0c111a;
    overflow: hidden;
    margin-bottom: 20px;
}}

.panel-title {{
    padding: 16px 20px;
    border-bottom: 1px solid #202838;
    font-weight: 700;
}}

pre {{
    margin: 0;
    padding: 22px;
    overflow-x: auto;
    font-family:
        "JetBrains Mono",
        "Fira Code",
        Consolas,
        monospace;
    font-size: 13px;
    line-height: 1.65;
    color: #d8e0ef;
    background: #070a10;
}}

ol {{
    margin: 0;
    padding: 20px 45px;
    color: #c8d1df;
}}

.footer {{
    text-align: center;
    color: #667085;
    font-size: 12px;
    padding: 20px;
}}

.badge {{
    display: inline-block;
    padding: 5px 10px;
    border-radius: 999px;
    background: #151d2b;
    color: #8ea7ff;
    font-size: 12px;
    margin-top: 12px;
}}

@media(max-width:700px) {{
    .container {{
        width: 96%;
        margin: 12px auto;
    }}

    .header {{
        padding: 20px;
    }}

    pre {{
        font-size: 12px;
    }}
}}
</style>
</head>

<body>

<div class="container">

    <div class="header">
        <h1>Lua Deobfuscator</h1>

        <p>
            Статический анализ и безопасное преобразование Lua-кода
        </p>

        <span class="badge">
            {html.escape(filename)}
        </span>
    </div>

    <div class="grid">

        <div class="stat">
            <span>Исходных строк</span>
            <strong>{original_lines}</strong>
        </div>

        <div class="stat">
            <span>Результирующих строк</span>
            <strong>{result_lines}</strong>
        </div>

        <div class="stat">
            <span>Размер исходника</span>
            <strong>{len(original.encode("utf-8")) // 1024} KB</strong>
        </div>

        <div class="stat">
            <span>Размер результата</span>
            <strong>{len(result.encode("utf-8")) // 1024} KB</strong>
        </div>

    </div>

    <div class="panel">

        <div class="panel-title">
            Выполненные преобразования
        </div>

        <ol>
            {passes_html}
        </ol>

    </div>

    <div class="panel">

        <div class="panel-title">
            Анализ
        </div>

        <div style="padding:20px">
            <div class="grid">
                {analysis_html}
            </div>
        </div>

    </div>

    <div class="panel">

        <div class="panel-title">
            Деобфусцированный код
        </div>

        <pre>{escape_code(result)}</pre>

    </div>

    <div class="panel">

        <div class="panel-title">
            Исходный код
        </div>

        <pre>{escape_code(original)}</pre>

    </div>

    <div class="footer">
        Lua Deobfuscator • Static analysis only
    </div>

</div>

</body>
</html>
"""


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Я Lua Deobfuscator.\n\n"
        "Отправь мне файл:\n"
        "• .lua\n"
        "• .txt\n\n"
        "Я выполню статический анализ, "
        "попробую разобрать распространённые "
        "слои обфускации и верну HTML-файл "
        "с результатом.\n\n"
        "⚠️ Загруженный Lua-код не выполняется."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 Поддерживаются:\n\n"
        "• Lua .lua\n"
        "• Lua .txt\n"
        "• string.char()\n"
        "• Lua decimal escapes\n"
        "• hexadecimal escapes\n"
        "• string.reverse()\n"
        "• простые конкатенации строк\n"
        "• базовое форматирование\n"
        "• статистика и анализ\n\n"
        "Максимальный размер файла: 5 MB."
    )


# ============================================================
# FILE HANDLER
# ============================================================

@dp.message(F.document)
async def receive_file(message: Message):

    document = message.document

    if not document:
        return

    filename = document.file_name or "script.lua"

    extension = Path(filename).suffix.lower()

    if extension not in {".lua", ".txt"}:
        await message.answer(
            "❌ Нужен файл .lua или .txt"
        )
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.answer(
            "❌ Файл слишком большой.\n"
            "Максимальный размер — 5 MB."
        )
        return

    status = await message.answer(
        "📥 Получаю файл..."
    )

    temp_dir = Path(tempfile.gettempdir())

    safe_name = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        filename
    )

    input_path = temp_dir / (
        f"lua_input_{message.from_user.id}_{safe_name}"
    )

    output_path = temp_dir / (
        f"lua_result_{message.from_user.id}.html"
    )

    try:

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        telegram_file = await bot.get_file(
            document.file_id
        )

        await bot.download_file(
            telegram_file.file_path,
            destination=input_path
        )

        await status.edit_text(
            "🔍 Анализирую Lua-код..."
        )

        # ----------------------------------------------------
        # READ
        # ----------------------------------------------------

        raw = input_path.read_bytes()

        if len(raw) > MAX_FILE_SIZE:
            raise ValueError(
                "File is larger than allowed size"
            )

        # Try UTF-8 first
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            source = raw.decode(
                "utf-8",
                errors="replace"
            )

        # ----------------------------------------------------
        # ANALYZE
        # ----------------------------------------------------

        analysis = analyze_code(source)

        await status.edit_text(
            "🧩 Выполняю преобразования..."
        )

        # ----------------------------------------------------
        # DEOBFUSCATE
        # ----------------------------------------------------

        result, passes = deobfuscate(source)

        # ----------------------------------------------------
        # HTML
        # ----------------------------------------------------

        html_content = create_html(
            original=source,
            result=result,
            analysis=analysis,
            passes=passes,
            filename=filename
        )

        output_path.write_text(
            html_content,
            encoding="utf-8"
        )

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        await status.edit_text(
            "✅ Готово! Отправляю результат..."
        )

        result_file = FSInputFile(
            output_path,
            filename=f"{Path(filename).stem}_deobfuscated.html"
        )

        await message.answer_document(
            result_file,
            caption=(
                "✅ Анализ завершён\n\n"
                f"📄 Файл: {filename}\n"
                f"📏 Строк: {count_lines(source)}\n"
                f"🧩 Преобразований: {len(passes)}\n\n"
                "⚠️ Результат получен статическим "
                "анализом. Lua-код не выполнялся."
            )
        )

        await status.delete()

    except Exception as e:

        logger.exception(
            "Error processing file: %s",
            e
        )

        await status.edit_text(
            "❌ Не удалось обработать файл.\n\n"
            "Попробуй другой Lua-файл."
        )

    finally:

        try:
            if input_path.exists():
                input_path.unlink()
        except Exception:
            pass

        try:
            if output_path.exists():
                output_path.unlink()
        except Exception:
            pass


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback(message: Message):

    await message.answer(
        "📄 Отправь мне Lua-файл как документ.\n\n"
        "Поддерживаются .lua и .txt."
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info("Starting Lua Deobfuscator Bot")

    await start_web_server()

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info("Bot polling started")

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")