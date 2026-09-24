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

# Максимальный размер Lua-файла
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing"
    )


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
# RENDER HTTP SERVER
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

    logger.info(
        "HTTP server started on port %s",
        PORT
    )


# ============================================================
# FILE READING
# ============================================================

def read_lua_file(path: Path) -> str:
    """
    Нормальное чтение Lua-файлов.

    Пробуем:
        UTF-8
        UTF-8 BOM
        UTF-16
        CP1251
        Latin-1

    Код никогда не выполняется.
    """

    raw = path.read_bytes()

    if not raw:
        raise ValueError(
            "Файл пустой."
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

    best_text = None
    best_score = -1

    for encoding in encodings:

        try:
            text = raw.decode(encoding)

        except UnicodeDecodeError:
            continue

        # Простейшая оценка качества декодирования.
        bad = text.count("\ufffd")
        nulls = text.count("\x00")

        score = (
            len(text)
            - bad * 100
            - nulls * 100
        )

        if score > best_score:
            best_score = score
            best_text = text

    if best_text is None:
        best_text = raw.decode(
            "utf-8",
            errors="replace"
        )

    # Убираем BOM
    best_text = best_text.lstrip("\ufeff")

    # Нормализуем окончания строк
    best_text = best_text.replace(
        "\r\n",
        "\n"
    )

    best_text = best_text.replace(
        "\r",
        "\n"
    )

    return best_text


# ============================================================
# BASIC STATS
# ============================================================

def count_lines(code: str) -> int:
    if not code:
        return 0

    return len(code.splitlines())


def analyze_code(code: str) -> dict:

    patterns = {
        "string.char()":
            r"\bstring\s*\.\s*char\s*\(",

        "string.byte()":
            r"\bstring\s*\.\s*byte\s*\(",

        "string.reverse()":
            r"\bstring\s*\.\s*reverse\s*\(",

        "string.rep()":
            r"\bstring\s*\.\s*rep\s*\(",

        "loadstring()":
            r"\bloadstring\s*\(",

        "load()":
            r"\bload\s*\(",

        "table.concat()":
            r"\btable\s*\.\s*concat\s*\(",

        "getfenv()":
            r"\bgetfenv\s*\(",

        "setfenv()":
            r"\bsetfenv\s*\(",

        "decimal escapes":
            r"\\\d{1,3}",

        "hex escapes":
            r"\\x[0-9a-fA-F]{2}",
    }

    result = {}

    for name, pattern in patterns.items():

        result[name] = len(
            re.findall(
                pattern,
                code
            )
        )

    indicators = [
        "obfusc",
        "decode",
        "decrypt",
        "encoded",
        "encrypt",
        "base64",
        "virtual",
        "vm",
        "xor",
    ]

    lower = code.lower()

    result["possible indicators"] = sum(
        lower.count(x)
        for x in indicators
    )

    return result


# ============================================================
# LUA STRING ESCAPES
# ============================================================

def decode_decimal_escapes(value: str) -> str:

    def repl(match):

        number = match.group(1)

        try:

            n = int(number)

            if 0 <= n <= 255:
                return chr(n)

        except Exception:
            pass

        return match.group(0)

    return re.sub(
        r"\\([0-9]{1,3})",
        repl,
        value
    )


def decode_hex_escapes(value: str) -> str:

    def repl(match):

        try:

            return chr(
                int(
                    match.group(1),
                    16
                )
            )

        except Exception:
            return match.group(0)

    return re.sub(
        r"\\x([0-9a-fA-F]{2})",
        repl,
        value
    )


def decode_common_escapes(value: str) -> str:

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

        value = value.replace(
            old,
            new
        )

    value = decode_decimal_escapes(
        value
    )

    value = decode_hex_escapes(
        value
    )

    return value


# ============================================================
# QUOTED STRINGS
# ============================================================

def decode_quoted_strings(code: str) -> str:

    pattern = re.compile(
        r'"(?:\\.|[^"\\])*"'
        r"|"
        r"'(?:\\.|[^'\\])*'"
    )

    def repl(match):

        original = match.group(0)

        quote = original[0]

        content = original[1:-1]

        decoded = decode_common_escapes(
            content
        )

        return (
            quote
            + decoded
            + quote
        )

    return pattern.sub(
        repl,
        code
    )


# ============================================================
# STRING.CHAR
# ============================================================

def parse_number(value: str):

    value = value.strip()

    try:

        if value.lower().startswith("0x"):

            return int(
                value,
                16
            )

        return int(
            value,
            10
        )

    except Exception:

        return None


def decode_string_char(code: str) -> str:

    pattern = re.compile(
        r"string\s*\.\s*char"
        r"\s*\(\s*"
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

            n = parse_number(
                part
            )

            if n is None:
                return match.group(0)

            if not 0 <= n <= 255:
                return match.group(0)

            numbers.append(n)

        if not numbers:
            return match.group(0)

        try:

            decoded = "".join(
                chr(x)
                for x in numbers
            )

            decoded = decoded.replace(
                "\\",
                "\\\\"
            )

            decoded = decoded.replace(
                '"',
                '\\"'
            )

            return (
                '"'
                + decoded
                + '"'
            )

        except Exception:

            return match.group(0)

    return pattern.sub(
        repl,
        code
    )


# ============================================================
# STRING.REVERSE
# ============================================================

def decode_string_reverse(code: str) -> str:

    pattern = re.compile(
        r'string\s*\.\s*reverse'
        r'\s*\(\s*'
        r'(["\'])'
        r'(.*?)'
        r'\1'
        r'\s*\)'
    )

    def repl(match):

        quote = match.group(1)

        value = match.group(2)

        return (
            quote
            + value[::-1]
            + quote
        )

    return pattern.sub(
        repl,
        code
    )


# ============================================================
# SIMPLE STRING CONCAT
# ============================================================

def decode_simple_concat(code: str) -> str:

    pattern = re.compile(
        r'(["\'])'
        r'([^"\']*)'
        r'\1'
        r'\s*\.\.\s*'
        r'(["\'])'
        r'([^"\']*)'
        r'\3'
    )

    changed = True

    while changed:

        new_code = pattern.sub(
            lambda m:
                '"'
                + m.group(2)
                + m.group(4)
                + '"',
            code
        )

        changed = (
            new_code != code
        )

        code = new_code

    return code


# ============================================================
# WHITESPACE
# ============================================================

def normalize_whitespace(code: str) -> str:

    code = code.replace(
        "\r\n",
        "\n"
    )

    code = code.replace(
        "\r",
        "\n"
    )

    lines = []

    for line in code.splitlines():

        line = line.rstrip()

        lines.append(
            line
        )

    return "\n".join(
        lines
    )


# ============================================================
# BASIC LUA FORMATTER
# ============================================================

def basic_indent(code: str) -> str:

    lines = code.splitlines()

    result = []

    indent = 0

    for line in lines:

        stripped = line.strip()

        if not stripped:

            result.append("")

            continue

        lower = stripped.lower()

        # Decrease indentation BEFORE output
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
            + stripped
        )

        # Comments don't affect indentation
        if stripped.startswith("--"):
            continue

        # elseif / else
        if (
            lower.startswith("else")
            or lower.startswith("elseif")
        ):

            indent += 1

            continue

        # then
        if lower.endswith("then"):

            indent += 1

            continue

        # do
        if (
            lower.endswith(" do")
            or lower == "do"
        ):

            indent += 1

            continue

        # repeat
        if lower == "repeat":

            indent += 1

            continue

        # function
        if (
            lower.startswith("function ")
            or lower.startswith("local function ")
        ):

            indent += 1

            continue

    return "\n".join(
        result
    )


# ============================================================
# DEOBFUSCATION PIPELINE
# ============================================================

def deobfuscate(code: str):

    passes = []

    # --------------------------------------------------------
    # PASS 1
    # --------------------------------------------------------

    old = code

    code = decode_quoted_strings(
        code
    )

    if code != old:

        passes.append(
            "decoded Lua string escapes"
        )

    # --------------------------------------------------------
    # PASS 2
    # --------------------------------------------------------

    old = code

    code = decode_string_char(
        code
    )

    if code != old:

        passes.append(
            "decoded string.char()"
        )

    # --------------------------------------------------------
    # PASS 3
    # --------------------------------------------------------

    old = code

    code = decode_string_reverse(
        code
    )

    if code != old:

        passes.append(
            "decoded string.reverse()"
        )

    # --------------------------------------------------------
    # PASS 4
    # --------------------------------------------------------

    old = code

    code = decode_simple_concat(
        code
    )

    if code != old:

        passes.append(
            "merged constant string concatenations"
        )

    # --------------------------------------------------------
    # PASS 5
    # --------------------------------------------------------

    old = code

    code = normalize_whitespace(
        code
    )

    if code != old:

        passes.append(
            "normalized whitespace"
        )

    # --------------------------------------------------------
    # PASS 6
    # --------------------------------------------------------

    old = code

    code = basic_indent(
        code
    )

    if code != old:

        passes.append(
            "formatted Lua indentation"
        )

    return code, passes


# ============================================================
# HTML
# ============================================================

def escape_code(code: str) -> str:

    return html.escape(
        code,
        quote=False
    )


def create_html(
    original,
    result,
    analysis,
    passes,
    filename
):

    original_lines = count_lines(
        original
    )

    result_lines = count_lines(
        result
    )

    original_size = len(
        original.encode(
            "utf-8"
        )
    )

    result_size = len(
        result.encode(
            "utf-8"
        )
    )

    stats = ""

    stats += f"""
    <div class="card">
        <small>Исходных строк</small>
        <b>{original_lines}</b>
    </div>
    """

    stats += f"""
    <div class="card">
        <small>Результирующих строк</small>
        <b>{result_lines}</b>
    </div>
    """

    stats += f"""
    <div class="card">
        <small>Исходный размер</small>
        <b>{original_size // 1024} KB</b>
    </div>
    """

    stats += f"""
    <div class="card">
        <small>Результат</small>
        <b>{result_size // 1024} KB</b>
    </div>
    """

    analysis_html = ""

    for key, value in analysis.items():

        analysis_html += f"""
        <div class="analysis-item">
            <span>
                {html.escape(str(key))}
            </span>

            <strong>
                {html.escape(str(value))}
            </strong>
        </div>
        """

    if passes:

        passes_html = "".join(
            f"<li>{html.escape(x)}</li>"
            for x in passes
        )

    else:

        passes_html = (
            "<li>"
            "Автоматических преобразований "
            "не найдено."
            "</li>"
        )

    return f"""<!DOCTYPE html>

<html lang="ru">

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width, initial-scale=1.0">

<title>
Lua Deobfuscator -
{html.escape(filename)}
</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{

    margin: 0;

    background:
        #070a0f;

    color:
        #e9eef7;

    font-family:
        Arial,
        Helvetica,
        sans-serif;
}}

.container {{

    width:
        min(1400px, 95%);

    margin:
        25px auto;
}}

.header {{

    padding:
        28px;

    border:
        1px solid #202938;

    border-radius:
        22px;

    background:
        linear-gradient(
            135deg,
            #111827,
            #0b1018
        );

    margin-bottom:
        18px;
}}

.header h1 {{

    margin:
        0 0 8px;

    font-size:
        28px;
}}

.header p {{

    margin:
        0;

    color:
        #8d98a8;
}}

.file {{

    display:
        inline-block;

    margin-top:
        14px;

    padding:
        7px 12px;

    border-radius:
        999px;

    background:
        #151d2a;

    color:
        #91a8ff;

    font-size:
        12px;
}}

.stats {{

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                180px,
                1fr
            )
        );

    gap:
        12px;

    margin-bottom:
        18px;
}}

.card {{

    padding:
        18px;

    background:
        #0d121b;

    border:
        1px solid #202938;

    border-radius:
        17px;
}}

.card small {{

    display:
        block;

    color:
        #818c9f;

    margin-bottom:
        8px;
}}

.card b {{

    font-size:
        22px;
}}

.panel {{

    background:
        #0b1018;

    border:
        1px solid #202938;

    border-radius:
        20px;

    overflow:
        hidden;

    margin-bottom:
        18px;
}}

.title {{

    padding:
        16px 20px;

    border-bottom:
        1px solid #202938;

    font-weight:
        bold;
}}

.analysis {{

    padding:
        18px;

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                220px,
                1fr
            )
        );

    gap:
        10px;
}}

.analysis-item {{

    display:
        flex;

    justify-content:
        space-between;

    padding:
        12px;

    border:
        1px solid #1b2432;

    border-radius:
        12px;

    background:
        #0e141e;
}}

.analysis-item span {{

    color:
        #929daf;
}}

ol {{

    margin:
        0;

    padding:
        20px 45px;

    color:
        #cbd4e2;
}}

pre {{

    margin:
        0;

    padding:
        22px;

    overflow-x:
        auto;

    background:
        #06090e;

    color:
        #dce5f4;

    font-family:
        Consolas,
        "Courier New",
        monospace;

    font-size:
        13px;

    line-height:
        1.65;

    tab-size:
        4;
}}

.footer {{

    text-align:
        center;

    color:
        #5f6a7b;

    font-size:
        12px;

    padding:
        15px;
}}

</style>

</head>

<body>

<div class="container">

<div class="header">

<h1>
Lua Deobfuscator
</h1>

<p>
Статический анализ Lua-кода
</p>

<div class="file">
{html.escape(filename)}
</div>

</div>


<div class="stats">

{stats}

</div>


<div class="panel">

<div class="title">
Выполненные преобразования
</div>

<ol>
{passes_html}
</ol>

</div>


<div class="panel">

<div class="title">
Анализ кода
</div>

<div class="analysis">

{analysis_html}

</div>

</div>


<div class="panel">

<div class="title">
Деобфусцированный код
</div>

<pre>
{escape_code(result)}
</pre>

</div>


<div class="panel">

<div class="title">
Исходный код
</div>

<pre>
{escape_code(original)}
</pre>

</div>


<div class="footer">

Lua Deobfuscator
•
Static analysis only

</div>

</div>

</body>

</html>
"""


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def start_command(
    message: Message
):

    await message.answer(
        "👋 Привет!\n\n"

        "Я Lua Deobfuscator.\n\n"

        "📄 Отправь мне Lua-файл "
        "в формате .lua или .txt.\n\n"

        "Я:\n"
        "🔎 прочитаю файл;\n"
        "🧩 проанализирую код;\n"
        "🧹 попробую убрать простые "
        "слои обфускации;\n"
        "🌐 создам HTML;\n"
        "📎 отправлю результат.\n\n"

        "⚠️ Загруженный Lua-код "
        "не выполняется."
    )


# ============================================================
# /HELP
# ============================================================

@dp.message(Command("help"))
async def help_command(
    message: Message
):

    await message.answer(
        "📖 Поддержка:\n\n"

        "• .lua\n"
        "• .txt\n"
        "• UTF-8\n"
        "• UTF-8 BOM\n"
        "• UTF-16\n"
        "• CP1251\n\n"

        "Обработка:\n"
        "• string.char()\n"
        "• string.reverse()\n"
        "• decimal escapes\n"
        "• hex escapes\n"
        "• простая конкатенация строк\n"
        "• форматирование Lua\n"
        "• анализ признаков обфускации\n\n"

        f"Максимальный размер: "
        f"{MAX_FILE_SIZE // 1024 // 1024} MB."
    )


# ============================================================
# FILE HANDLER
# ============================================================

@dp.message(F.document)
async def receive_file(
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

    # --------------------------------------------------------
    # EXTENSION
    # --------------------------------------------------------

    if extension not in {
        ".lua",
        ".txt"
    }:

        await message.answer(
            "❌ Поддерживаются только "
            ".lua и .txt"
        )

        return

    # --------------------------------------------------------
    # SIZE
    # --------------------------------------------------------

    if (
        document.file_size
        and
        document.file_size
        > MAX_FILE_SIZE
    ):

        await message.answer(
            "❌ Файл слишком большой.\n\n"
            f"Максимум: "
            f"{MAX_FILE_SIZE // 1024 // 1024} MB."
        )

        return

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    status = await message.answer(
        "📥 Получаю файл..."
    )

    temp_dir = Path(
        tempfile.gettempdir()
    )

    safe_name = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        filename
    )

    user_id = (
        message.from_user.id
        if message.from_user
        else 0
    )

    input_path = (
        temp_dir
        /
        f"lua_{user_id}_{safe_name}"
    )

    output_path = (
        temp_dir
        /
        f"lua_result_{user_id}.html"
    )

    try:

        # ====================================================
        # DOWNLOAD
        # ====================================================

        logger.info(
            "Receiving file: %s",
            filename
        )

        telegram_file = (
            await bot.get_file(
                document.file_id
            )
        )

        if not telegram_file.file_path:

            raise RuntimeError(
                "Telegram не вернул "
                "путь к файлу."
            )

        await bot.download_file(
            telegram_file.file_path,
            destination=str(
                input_path
            )
        )

        # ====================================================
        # VERIFY
        # ====================================================

        if not input_path.exists():

            raise RuntimeError(
                "Файл не был скачан."
            )

        actual_size = (
            input_path.stat()
            .st_size
        )

        logger.info(
            "Downloaded %s bytes",
            actual_size
        )

        if actual_size == 0:

            raise ValueError(
                "Файл пустой."
            )

        if actual_size > MAX_FILE_SIZE:

            raise ValueError(
                "Файл превышает лимит."
            )

        # ====================================================
        # READ
        # ====================================================

        await status.edit_text(
            "📖 Читаю Lua-файл..."
        )

        source = read_lua_file(
            input_path
        )

        logger.info(
            "Read Lua source: %s chars",
            len(source)
        )

        if not source.strip():

            raise ValueError(
                "После чтения файл оказался пустым."
            )

        # ====================================================
        # ANALYSIS
        # ====================================================

        await status.edit_text(
            "🔎 Анализирую код..."
        )

        analysis = analyze_code(
            source
        )

        logger.info(
            "Analysis: %s",
            analysis
        )

        # ====================================================
        # DEOBFUSCATION
        # ====================================================

        await status.edit_text(
            "🧩 Выполняю деобфускацию..."
        )

        result, passes = (
            deobfuscate(
                source
            )
        )

        logger.info(
            "Deobfuscation passes: %s",
            passes
        )

        if not result:

            raise RuntimeError(
                "Результат деобфускации пуст."
            )

        # ====================================================
        # HTML
        # ====================================================

        await status.edit_text(
            "🌐 Создаю HTML-файл..."
        )

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

        if not output_path.exists():

            raise RuntimeError(
                "HTML-файл не создан."
            )

        # ====================================================
        # SEND
        # ====================================================

        await status.edit_text(
            "📤 Отправляю результат..."
        )

        result_file = FSInputFile(
            path=str(
                output_path
            ),
            filename=(
                f"{Path(filename).stem}"
                f"_deobfuscated.html"
            )
        )

        await message.answer_document(
            document=result_file,
            caption=(
                "✅ Готово!\n\n"

                f"📄 Файл: {filename}\n"
                f"📏 Строк: "
                f"{count_lines(source)}\n"
                f"🧩 Преобразований: "
                f"{len(passes)}\n\n"

                "⚠️ Код не выполнялся."
            )
        )

        await status.delete()

        logger.info(
            "Successfully processed: %s",
            filename
        )

    except Exception as e:

        logger.exception(
            "PROCESSING ERROR: %s",
            filename
        )

        error = str(e).strip()

        if not error:
            error = type(e).__name__

        error = error[:1000]

        try:

            await status.edit_text(
                "❌ Не удалось обработать файл.\n\n"
                f"Тип ошибки: "
                f"{type(e).__name__}\n\n"
                f"Причина:\n"
                f"{error}\n\n"
                "Подробности находятся "
                "в Render Logs."
            )

        except Exception:

            await message.answer(
                "❌ Ошибка обработки:\n\n"
                f"{error}"
            )

    finally:

        # ====================================================
        # DELETE TEMP FILES
        # ====================================================

        try:

            if input_path.exists():
                input_path.unlink()

        except Exception as e:

            logger.warning(
                "Input cleanup failed: %s",
                e
            )

        try:

            if output_path.exists():
                output_path.unlink()

        except Exception as e:

            logger.warning(
                "Output cleanup failed: %s",
                e
            )


# ============================================================
# OTHER MESSAGES
# ============================================================

@dp.message()
async def other_message(
    message: Message
):

    await message.answer(
        "📄 Отправь Lua-файл как документ.\n\n"
        "Поддерживаются:\n"
        "• .lua\n"
        "• .txt"
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "Starting Lua Deobfuscator..."
    )

    await start_web_server()

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info(
        "Telegram polling started"
    )

    await dp.start_polling(
        bot
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped"
        )

    except Exception:

        logger.exception(
            "Fatal application error"
        )
