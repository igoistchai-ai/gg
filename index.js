const { Telegraf, Markup } = require("telegraf");
const http = require("http");
require("dotenv").config();

const BOT_TOKEN = process.env.BOT_TOKEN;

if (!BOT_TOKEN) {
  console.error("Ошибка: BOT_TOKEN не найден в Environment Variables");
  process.exit(1);
}

const bot = new Telegraf(BOT_TOKEN);

//
// КАРТИНКИ
// Можно оставить пустыми.
// Для Telegram нужны прямые ссылки на изображения или file_id.
//
const IMG_START = process.env.IMG_START || "";
const IMG_CONFIRM = process.env.IMG_CONFIRM || "";
const IMG_SEARCH = process.env.IMG_SEARCH || "";
const IMG_SUCCESS = process.env.IMG_SUCCESS || "";
const IMG_FAIL = process.env.IMG_FAIL || "";

//
// Состояния пользователей
//
const users = new Map();

//
// Проверка email
//
function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/i.test(email);
}

//
// Защита HTML
//
function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

//
// Отправка картинки.
// Если картинка не задана или Telegram не смог её загрузить,
// бот автоматически отправит обычное сообщение.
//
async function sendImage(ctx, image, caption, extra = {}) {
  if (image) {
    try {
      return await ctx.replyWithPhoto(image, {
        caption,
        ...extra
      });
    } catch (error) {
      console.log("Не удалось отправить изображение:", error.message);
    }
  }

  return await ctx.reply(caption, extra);
}

//
// Главное меню
//
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

//
// /start
//
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

//
// Кнопка проверки
//
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
        Markup.button.callback("Да", "confirm_yes")
      ],
      [
        Markup.button.callback("Отмена", "confirm_no")
      ]
    ])
  );
});

//
// Подтверждение
//
bot.action("confirm_yes", async (ctx) => {
  await ctx.answerCbQuery().catch(() => {});

  users.set(ctx.chat.id, {
    step: "await_email"
  });

  await ctx.reply(
    "Введите email:"
  );
});

//
// Отмена
//
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

//
// Получение email
//
bot.on("text", async (ctx) => {
  const chatId = ctx.chat.id;
  const state = users.get(chatId);

  if (!state) {
    return;
  }

  if (state.step !== "await_email") {
    return;
  }

  const email = ctx.message.text.trim().toLowerCase();

  //
  // Проверяем формат
  //
  if (!isValidEmail(email)) {
    await ctx.reply(
      "Некорректный email.\n\nПопробуйте ещё раз:"
    );

    return;
  }

  users.set(chatId, {
    step: "searching"
  });

  //
  // Экран поиска
  //
  await sendImage(
    ctx,
    IMG_SEARCH,
    "Начинаю проверку..."
  );

  //
  // Небольшая задержка для отображения процесса
  //
  await new Promise((resolve) => {
    setTimeout(resolve, 1000);
  });

  const domain = email.split("@")[1];

  //
  // Короткий результат.
  // Здесь намеренно нет огромного JSON и перебора
  // сторонних аккаунтов.
  //
  const resultText =
    "<b>Результат проверки</b>\n\n" +
    "Email: <code>" +
    escapeHtml(email) +
    "</code>\n" +
    "Домен: <code>" +
    escapeHtml(domain) +
    "</code>\n" +
    "Формат: корректный";

  await sendImage(
    ctx,
    IMG_SUCCESS,
    "Проверка завершена."
  );

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
            "probit_email"
          )
        ]
      ])
    }
  );

  users.set(chatId, {
    step: "start"
  });
});

//
// HTTP-сервер для Render
//
// Render Web Service требует открытый порт.
//
const PORT = Number(process.env.PORT) || 3000;

const server = http.createServer((req, res) => {
  if (req.url === "/" || req.url === "/health") {
    res.writeHead(200, {
      "Content-Type": "text/plain; charset=utf-8"
    });

    res.end("Bot is running");
    return;
  }

  res.writeHead(404, {
    "Content-Type": "text/plain; charset=utf-8"
  });

  res.end("Not Found");
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `HTTP server started on port ${PORT}`
  );
});

//
// Запуск Telegram-бота
//
bot.launch()
  .then(() => {
    console.log("Telegram bot started successfully");
  })
  .catch((error) => {
    console.error(
      "Telegram bot startup error:",
      error
    );

    process.exit(1);
  });

//
// Корректное завершение
//
process.once("SIGINT", () => {
  bot.stop("SIGINT");
  server.close();
});

process.once("SIGTERM", () => {
  bot.stop("SIGTERM");
  server.close();
});
