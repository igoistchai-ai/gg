import os
import io
import time
import asyncio
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
import uvicorn

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
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

# Сколько секунд живёт ссылка.
# 0 = бессрочно
LINK_TTL = int(os.getenv("LINK_TTL", "86400"))

# Максимальный размер Lua-файла: 5 MB
MAX_FILE_SIZE = 5 * 1024 * 1024

# ============================================================
# STORAGE
# ============================================================

BASE_DIR = Path("private_storage")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ============================================================
# RANDOM 404
# ============================================================

@app.exception_handler(404)
async def not_found(request: Request, exc):
    return PlainTextResponse(
        "404 Not Found",
        status_code=404,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


# ============================================================
# ROOT -> 404
# ============================================================

@app.get("/")
async def root():
    return PlainTextResponse(
        "404 Not Found",
        status_code=404
    )


# ============================================================
# SOURCE DELIVERY
# ============================================================

@app.get("/l/{token}")
async def get_script(token: str, request: Request):

    # Защита от мусорных токенов
    if len(token) != 96:
        return PlainTextResponse(
            "404 Not Found",
            status_code=404,
            headers={"Cache-Control": "no-store"}
        )

    # Разрешаем только безопасные символы
    if not all(
        c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in token
    ):
        return PlainTextResponse(
            "404 Not Found",
            status_code=404,
            headers={"Cache-Control": "no-store"}
        )

    file_path = BASE_DIR / f"{token}.lua"

    # Никаких сообщений вроде "файл существует"
    if not file_path.exists():
        return PlainTextResponse(
            "404 Not Found",
            status_code=404,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            }
        )

    try:
        # Проверяем TTL
        if LINK_TTL > 0:

            age = time.time() - file_path.stat().st_mtime

            if age > LINK_TTL:

                try:
                    file_path.unlink()
                except Exception:
                    pass

                return PlainTextResponse(
                    "404 Not Found",
                    status_code=404,
                    headers={
                        "Cache-Control": "no-store"
                    }
                )

        source = file_path.read_text(
            encoding="utf-8"
        )

    except Exception:
        return PlainTextResponse(
            "404 Not Found",
            status_code=404,
            headers={
                "Cache-Control": "no-store"
            }
        )

    # Не кэшируем исходник
    return Response(
        content=source,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
    )


# ============================================================
# EVERYTHING ELSE -> 404
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
        "HEAD"
    ]
)
async def catch_all(path: str):
    return PlainTextResponse(
        "404 Not Found",
        status_code=404,
        headers={
            "Cache-Control": "no-store"
        }
    )


# ============================================================
# TOKEN
# ============================================================

def create_token():
    """
    72 random bytes -> URL-safe token.
    Получается очень длинный непредсказуемый URL.
    """

    return secrets.token_urlsafe(72)


# ============================================================
# SAVE SCRIPT
# ============================================================

def save_script(source: str):

    token = create_token()

    path = BASE_DIR / f"{token}.lua"

    # Дополнительная проверка
    if len(source.encode("utf-8")) > MAX_FILE_SIZE:
        raise ValueError("File too large")

    path.write_text(
        source,
        encoding="utf-8"
    )

    return token


# ============================================================
# DELETE OLD FILES
# ============================================================

def cleanup_old_files():

    if LINK_TTL <= 0:
        return

    now = time.time()

    try:
        for path in BASE_DIR.glob("*.lua"):

            try:
                age = now - path.stat().st_mtime

                if age > LINK_TTL:
                    path.unlink()

            except Exception:
                pass

    except Exception:
        pass


# ============================================================
# LOADER
# ============================================================

def make_loader(url: str):

    # Loader специально минимальный.
    #
    # Сервер хранит настоящий Lua-код.
    # Loader получает его по уникальному URL.

    loader = f'''-- Generated loader
-- Do not edit

local URL = {url!r}

local ok, source = pcall(function()
    return game:HttpGet(URL)
end)

if not ok then
    error("Failed to retrieve script")
end

if type(source) ~= "string" or #source == 0 then
    error("Empty script")
end

local execute, compileError = loadstring(source)

if not execute then
    error(compileError or "Failed to compile script")
end

local success, runtimeError = pcall(execute)

if not success then
    error(runtimeError)
end
'''

    return loader


# ============================================================
# TELEGRAM /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Отправь мне файл .lua, и я создам для него уникальный loader."
    )


# ============================================================
# TELEGRAM /help
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "Отправь .lua файл.\n\n"
        "Бот автоматически создаст приватный URL "
        "и вернёт готовый loader.lua."
    )


# ============================================================
# FILE PROCESSING
# ============================================================

async def process_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    if not message:
        return

    document = message.document

    if not document:
        return

    filename = document.file_name or "script.lua"

    # Только Lua
    if not filename.lower().endswith(".lua"):
        await message.reply_text(
            "❌ Нужен файл с расширением .lua"
        )
        return

    # Размер
    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.reply_text(
            "❌ Файл слишком большой. Максимум 5 MB."
        )
        return

    status = await message.reply_text(
        "⏳ Загружаю и создаю приватную ссылку..."
    )

    try:

        tg_file = await document.get_file()

        data = io.BytesIO()

        await tg_file.download_to_memory(
            data
        )

        raw = data.getvalue()

        if len(raw) > MAX_FILE_SIZE:
            await status.edit_text(
                "❌ Файл слишком большой."
            )
            return

        # Проверяем UTF-8
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            await status.edit_text(
                "❌ Файл должен быть UTF-8."
            )
            return

        # Сохраняем
        token = await asyncio.to_thread(
            save_script,
            source
        )

        # Render предоставляет внешний URL
        base_url = os.getenv(
            "RENDER_EXTERNAL_URL",
            ""
        ).strip().rstrip("/")

        # Если Render URL не найден,
        # можно указать PUBLIC_URL вручную
        if not base_url:

            base_url = os.getenv(
                "PUBLIC_URL",
                ""
            ).strip().rstrip("/")

        if not base_url:

            await status.edit_text(
                "❌ Не найден PUBLIC_URL / RENDER_EXTERNAL_URL."
            )
            return

        script_url = (
            f"{base_url}/l/{token}"
        )

        loader = make_loader(
            script_url
        )

        loader_file = io.BytesIO(
            loader.encode("utf-8")
        )

        loader_file.name = (
            "loader.lua"
        )

        await status.delete()

        await message.reply_document(
            document=loader_file,
            caption=(
                "✅ Loader создан\n\n"
                f"⏱ Срок ссылки: "
                f"{'без срока' if LINK_TTL <= 0 else f'{LINK_TTL // 3600} ч.'}\n\n"
                "Исходник не находится в публичной папке сервера."
            )
        )

    except Exception as e:

        print(
            "PROCESS ERROR:",
            repr(e)
        )

        try:
            await status.edit_text(
                "❌ Не удалось обработать файл."
            )
        except Exception:
            pass


# ============================================================
# TEXT
# ============================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📁 Отправь файл .lua"
    )


# ============================================================
# CLEANUP THREAD
# ============================================================

def cleanup_loop():

    while True:

        try:
            cleanup_old_files()

        except Exception as e:
            print(
                "CLEANUP ERROR:",
                repr(e)
            )

        # Проверяем раз в час
        time.sleep(3600)


# ============================================================
# FASTAPI THREAD
# ============================================================

def run_server():

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="warning"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 50)
    print("Lua Loader Server")
    print("=" * 50)

    print(
        "PORT:",
        PORT
    )

    print(
        "TTL:",
        LINK_TTL
    )

    print(
        "Storage:",
        str(BASE_DIR.absolute())
    )

    # Web server
    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )

    server_thread.start()

    # Cleanup
    cleanup_thread = threading.Thread(
        target=cleanup_loop,
        daemon=True
    )

    cleanup_thread.start()

    # Telegram
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
        CommandHandler(
            "help",
            help_command
        )
    )

    bot.add_handler(
        MessageHandler(
            filters.Document.ALL,
            process_file
        )
    )

    bot.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message
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
