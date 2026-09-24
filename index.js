const { Telegraf, Markup } = require("telegraf");
const http = require("http");
require("dotenv").config();

const BOT_TOKEN = process.env.BOT_TOKEN;

if (!BOT_TOKEN) {
  console.error("❌ BOT_TOKEN не найден в Environment Variables");
  process.exit(1);
}

const bot = new Telegraf(BOT_TOKEN);

// =====================================================
// КАРТИНКИ
// ВАЖНО: это твои ссылки. ENV для картинок НЕ нужен.
// Если Telegram не примет страницу ibb.co,
// sendImage автоматически отправит обычный текст.
// =====================================================

const IMG_START = "https://ibb.co/DHv4fmT5";
const IMG_CONFIRM = "https://ibb.co/ycVd98zr";
const IMG_SEARCH = "https://ibb.co/dsGy2BDh";
const IMG_SUCCESS = "https://ibb.co/84cfh00b";
const IMG_FAIL = "https://ibb.co/V0bdcbDX";

// =====================================================
// СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ
// =====================================================

const users = new Map();

// =====================================================
// ПРОВЕРКА EMAIL
// =====================================================

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/i.test(email);
}

// =====================================================
// ЗАЩИТА HTML
// =====================================================

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// =====================================================
// ОТПРАВКА КАРТИНКИ
// =====================================================

async function sendImage(ctx, image, caption, extra = {}) {
  if (image) {
    try {
      await ctx.replyWithPhoto(
        { url: image },
        {
          caption,
          ...extra
        }
      );

      return;
    } catch (error) {
      console.log(
        "⚠️ Картинка не загрузилась:",
        error.message
      );
    }
  }

  await ctx.reply(caption, extra);
}

// =====================================================
// ГЛАВНОЕ МЕНЮ
// =====================================================

function mainMenu() {
  return Markup.inlineKeyboard([
    [
      Markup.button.callback(
        "Проверка электронной почты",
        "probit_email"
      )
    ]
  ]);
}

// =====================================================
// /START
// =====================================================

bot.start(async (ctx) => {
  users.set(ctx.chat.id, {
    step: "start"
  });

  await sendImage(
    ctx,
    IMG_START,
    "Начни получать информацию тут.",
    mainMenu()
  );
});

// =====================================================
// ПРОВЕРКА EMAIL
// =====================================================

bot.action("probit_email", async (ctx) => {
  await ctx.answerCbQuery().catch(() => {});

  users.set(ctx.chat.id, {
    step: "confirm"
  });

  await sendImage(
    ctx,
    IMG_CONFIRM,
    "Вы точно подтверждаете свои действия?\n\nЕсли да — нажмите кнопку ниже.",
    Markup.inlineKeyboard([
      [
        Markup.button.callback(
          "Да",
          "confirm_yes"
        )
      ],
      [
        Markup.button.callback(
          "Отмена",
          "confirm_no"
        )
      ]
    ])
  );
});

// =====================================================
// ПОДТВЕРЖДЕНИЕ
// =====================================================

bot.action("confirm_yes", async (ctx) => {
  await ctx.answerCbQuery().catch(() => {});

  users.set(ctx.chat.id, {
    step: "await_email"
  });

  await ctx.reply("Введите email:");
});

// =====================================================
// ОТМЕНА
// =====================================================

bot.action("confirm_no", async (ctx) => {
  await ctx.answerCbQuery().catch(() => {});

  users.set(ctx.chat.id, {
    step: "start"
  });

  await ctx.reply(
    "Отменено.",
    mainMenu()
  );
});

// =====================================================
// НОВЫЙ ПОИСК
// =====================================================

bot.action("new_search", async (ctx) => {
  await ctx.answerCbQuery().catch(() => {});

  users.set(ctx.chat.id, {
    step: "await_email"
  });

  await ctx.reply("Введите email:");
});

// =====================================================
// ПОЛУЧЕНИЕ EMAIL
// =====================================================

bot.on("text", async (ctx) => {
  const chatId = ctx.chat.id;
  const state = users.get(chatId);

  if (!state) {
    return;
  }

  if (state.step !== "await_email") {
    return;
  }

  const email =
    ctx.message.text
      .trim()
      .toLowerCase();

  // -----------------------------------------------
  // ПРОВЕРКА ФОРМАТА
  // -----------------------------------------------

  if (!isValidEmail(email)) {
    await ctx.reply(
      "Некорректный email.\n\nПопробуйте ещё раз:"
    );

    return;
  }

  // -----------------------------------------------
  // СОСТОЯНИЕ ПОИСКА
  // -----------------------------------------------

  users.set(chatId, {
    step: "searching"
  });

  // -----------------------------------------------
  // КАРТИНКА ПОИСКА
  // -----------------------------------------------

  await sendImage(
    ctx,
    IMG_SEARCH,
    "Начинается проверка по базам..."
  );

  // -----------------------------------------------
  // ЗАДЕРЖКА
  // -----------------------------------------------

  await new Promise((resolve) => {
    setTimeout(resolve, 1000);
  });

  // -----------------------------------------------
  // СТАРАЯ ЛОГИКА
  // -----------------------------------------------

  const domain = email.split("@")[1];

  const resultText =
    "<b>Результат проверки</b>\n\n" +
    "Email: <code>" +
    escapeHtml(email) +
    "</code>\n" +
    "Домен: <code>" +
    escapeHtml(domain) +
    "</code>\n" +
    "Формат: корректный";

  // -----------------------------------------------
  // КАРТИНКА РЕЗУЛЬТАТА
  // -----------------------------------------------

  await sendImage(
    ctx,
    IMG_SUCCESS,
    "Проверка завершена."
  );

  // -----------------------------------------------
  // РЕЗУЛЬТАТ
  // -----------------------------------------------

  await ctx.reply(
    resultText,
    {
      parse_mode: "HTML",
      ...Markup.inlineKeyboard([
        [
          Markup.button.url(
            "Открыть почту",
            "https://mail.google.com/"
          )
        ],
        [
          Markup.button.callback(
            "Новый поиск",
            "new_search"
          )
        ]
      ])
    }
  );

  users.set(chatId, {
    step: "start"
  });
});

// =====================================================
// ОБРАБОТКА НЕПРЕДУСМОТРЕННЫХ ОШИБОК
// =====================================================

bot.catch((error, ctx) => {
  console.error(
    "Ошибка Telegram:",
    error?.message || error
  );
});

// =====================================================
// HTTP SERVER ДЛЯ RENDER
// =====================================================

const PORT =
  Number(process.env.PORT) || 3000;

const server = http.createServer(
  (req, res) => {
    if (
      req.url === "/" ||
      req.url === "/health"
    ) {
      res.writeHead(200, {
        "Content-Type":
          "text/plain; charset=utf-8"
      });

      res.end("Bot is running");
      return;
    }

    res.writeHead(404, {
      "Content-Type":
        "text/plain; charset=utf-8"
    });

    res.end("Not Found");
  }
);

server.listen(
  PORT,
  "0.0.0.0",
  () => {
    console.log(
      `HTTP server started on port ${PORT}`
    );
  }
);

// =====================================================
// ЗАПУСК TELEGRAM
// =====================================================

(async () => {
  try {
    console.log(
      "🚀 Запуск Telegram бота..."
    );

    await bot.launch({
      dropPendingUpdates: true
    });

    console.log(
      "✅ Telegram bot started successfully"
    );
  } catch (error) {
    console.error(
      "❌ Telegram bot startup error:",
      error
    );

    process.exit(1);
  }
})();

// =====================================================
// КОРРЕКТНОЕ ЗАВЕРШЕНИЕ
// =====================================================

process.once(
  "SIGINT",
  () => {
    bot.stop("SIGINT");
    server.close();
  }
);

process.once(
  "SIGTERM",
  () => {
    bot.stop("SIGTERM");
    server.close();
  }
);
