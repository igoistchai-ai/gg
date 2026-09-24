const { Telegraf, Markup } = require('telegraf');
const crypto = require('crypto');
require('dotenv').config();

const BOT_TOKEN = process.env.BOT_TOKEN;
if (!BOT_TOKEN) {
  console.error('BOT_TOKEN not set');
  process.exit(1);
}

const bot = new Telegraf(BOT_TOKEN);

// Картинки — замени на свои URL или file_id
const IMG_START = 'https://ibb.co/DHv4fmT5';
const IMG_CONFIRM = 'https://ibb.co/ycVd98zr';
const IMG_SEARCH = 'https://ibb.co/dsGy2BDh';
const IMG_SUCCESS = 'https://ibb.co/84cfh00b';
const IMG_FAIL = 'https://ibb.co/V0bdcbDX';

// Состояния пользователей (в памяти; для продакшена — Redis/БД)
const userState = new Map();

// Сервисы для проверки email
const services = [
  { name: 'Gravatar', method: 'GET', url: (email) => `https://gravatar.com/${md5(email)}.json`, check: (text) => text.includes('"entry"') },
  { name: 'Adobe', method: 'POST', url: () => 'https://auth.services.adobe.com/signin/v2/users/accounts', body: (email) => JSON.stringify({ email }), check: (text) => !text.includes('error') && text.trim().length > 0 },
  { name: 'Duolingo', method: 'GET', url: (email) => `https://www.duolingo.com/2017-06-30/users?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"users"') && !text.includes('"users":[]') },
  { name: 'Chess.com', method: 'GET', url: (email) => `https://www.chess.com/callback/email/available?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"available":false') },
  { name: 'Notion', method: 'POST', url: () => 'https://www.notion.so/api/v3/getLoginOptions', body: (email) => JSON.stringify({ email }), check: (text) => text.includes('"user"') },
  { name: 'Spotify', method: 'GET', url: (email) => `https://spclient.wg.spotify.com/signup/public/v1/account?validate=1&email=${encodeURIComponent(email)}`, check: (text) => text.includes('"status":200') },
  { name: 'Strava', method: 'GET', url: (email) => `https://www.strava.com/athletes/email_unique?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"exists":true') },
  { name: 'Twitter', method: 'GET', url: (email) => `https://api.twitter.com/i/users/email_available.json?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"valid":false') },
  { name: 'Xvideos', method: 'GET', url: (email) => `https://www.xvideos.com/account/checkemail?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"status":200') },
  { name: 'Picsart', method: 'GET', url: (email) => `https://api.picsart.com/users/email/existence?email_encoded=0&emails=${encodeURIComponent(email)}`, check: (text) => text.includes('"exists":true') },
  { name: 'Eventbrite', method: 'POST', url: () => 'https://www.eventbrite.com/api/v3/users/lookup/', body: (email) => JSON.stringify({ email }), check: (text) => text.includes('"user_id"') },
  { name: 'Imageshack', method: 'POST', url: () => 'https://imageshack.com/rest_api/v2/user', body: (email) => JSON.stringify({ email }), check: (text) => text.includes('"user"') },
  { name: 'GitHub', method: 'GET', url: (email) => `https://api.github.com/search/users?q=${encodeURIComponent(email)}`, check: (text) => text.includes('"total_count":0') === false },
  { name: 'Steam', method: 'GET', url: (email) => `https://store.steampowered.com/account/checkemailavailability/?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"is_available":false') },
  { name: 'Epic Games', method: 'GET', url: (email) => `https://www.epicgames.com/id/api/email/check?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"exists":true') },
  { name: 'Roblox', method: 'GET', url: (email) => `https://apis.roblox.com/email/validate?email=${encodeURIComponent(email)}`, check: (text) => text.includes('"isValid":true') },
  { name: 'Yandex', method: 'POST', url: () => 'https://passport.yandex.ru/registration/validations/email', body: (email) => JSON.stringify({ email }), check: (text) => text.includes('"status":"ok"') && text.includes('"exists":true') },
  { name: 'Microsoft', method: 'GET', url: (email) => `https://login.live.com/GetUserType.srf?json=1&username=${encodeURIComponent(email)}`, check: (text) => text.includes('"MemberName"') },
  { name: 'Tumblr', method: 'POST', url: () => 'https://www.tumblr.com/svc/account/register', body: (email) => JSON.stringify({ email }), check: (text) => text.includes('already') || text.includes('exists') },
  { name: 'Twitch', method: 'POST', url: () => 'https://passport.twitch.tv/accounts/v1/check_email', body: (email) => JSON.stringify({ email }), check: (text) => text.includes('"isEmailTaken":true') },
];

function md5(str) {
  return crypto.createHash('md5').update(str.trim().toLowerCase()).digest('hex');
}

async function checkEmail(email) {
  const results = [];
  for (const service of services) {
    try {
      const url = service.url(email);
      const options = {
        method: service.method,
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
          'Content-Type': 'application/json',
        },
      };
      if (service.body) {
        options.body = service.body(email);
      }
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      const resp = await fetch(url, { ...options, signal: controller.signal });
      clearTimeout(timeout);
      const text = await resp.text();
      if (resp.ok) {
        const found = service.check(text);
        results.push({
          name: service.name,
          url: url,
          status: found ? 'FOUND' : 'NOT_FOUND',
          metadata: found ? extractMetadata(text) : []
        });
      } else {
        results.push({
          name: service.name,
          url: url,
          status: 'NOT_FOUND',
          metadata: []
        });
      }
    } catch (e) {
      results.push({
        name: service.name,
        url: service.url(email),
        status: 'ERROR',
        metadata: []
      });
    }
  }
  return results;
}

function extractMetadata(text) {
  try {
    const json = JSON.parse(text);
    const meta = [];
    if (json.entry && json.entry[0]) {
      const e = json.entry[0];
      if (e.displayName) meta.push({ type: 'String', name: 'Name', value: e.displayName });
      if (e.preferredUsername) meta.push({ type: 'String', name: 'Username', value: e.preferredUsername });
      if (e.thumbnailUrl) meta.push({ type: 'Image', name: 'Avatar', value: e.thumbnailUrl });
    }
    return meta;
  } catch {
    return [];
  }
}

bot.start(async (ctx) => {
  const chatId = ctx.chat.id;
  userState.set(chatId, { step: 'start' });
  await ctx.replyWithPhoto(IMG_START, {
    caption: 'Начни получать инфу тут.',
    ...Markup.inlineKeyboard([
      [Markup.button.callback('🔍 Пробив электронной почты', 'probit_email')]
    ])
  });
});

bot.action('probit_email', async (ctx) => {
  const chatId = ctx.chat.id;
  userState.set(chatId, { step: 'confirm' });
  await ctx.replyWithPhoto(IMG_CONFIRM, {
    caption: 'Вы точно подтверждаете свои действия? Если да — нажмите кнопку ниже. Если нет — отмена.',
    ...Markup.inlineKeyboard([
      [Markup.button.callback('✅ Да', 'confirm_yes')],
      [Markup.button.callback('❌ Отмена', 'confirm_no')]
    ])
  });
});

bot.action('confirm_yes', async (ctx) => {
  const chatId = ctx.chat.id;
  userState.set(chatId, { step: 'await_email' });
  await ctx.reply('Введите email для поиска:');
});

bot.action('confirm_no', async (ctx) => {
  const chatId = ctx.chat.id;
  userState.set(chatId, { step: 'start' });
  await ctx.reply('Отменено.');
});

bot.on('text', async (ctx) => {
  const chatId = ctx.chat.id;
  const state = userState.get(chatId);
  if (state && state.step === 'await_email') {
    const email = ctx.message.text.trim();
    if (!isValidEmail(email)) {
      await ctx.reply('Некорректный email. Попробуйте ещё раз:');
      return;
    }
    userState.set(chatId, { step: 'searching' });
    await ctx.replyWithPhoto(IMG_SEARCH, { caption: '🔎 Ищем по 600+ источникам...' });

    const results = await checkEmail(email);
    const foundCount = results.filter(r => r.status === 'FOUND').length;
    const jsonOutput = JSON.stringify(results, null, 2);

    if (foundCount > 0) {
      await ctx.replyWithPhoto(IMG_SUCCESS, { caption: `✅ Найдено аккаунтов: ${foundCount}` });
    } else {
      await ctx.replyWithPhoto(IMG_FAIL, { caption: '❌ Ничего не найдено.' });
    }

    if (jsonOutput.length < 4000) {
      await ctx.reply(`<pre>${jsonOutput}</pre>`, { parse_mode: 'HTML' });
    } else {
      await ctx.replyWithDocument({ source: Buffer.from(jsonOutput), filename: 'result.json' });
    }

    userState.set(chatId, { step: 'start' });
  }
});

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

bot.launch();
console.log('Bot started');