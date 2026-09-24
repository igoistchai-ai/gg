const { Telegraf, Markup } = require("telegraf");
const http = require("http");
const dns = require("dns").promises;
const net = require("net");
const fs = require("fs");
const path = require("path");
require("dotenv").config();

/*
==========================================================
                    CONFIG
==========================================================
*/

const BOT_TOKEN = process.env.BOT_TOKEN;

if (!BOT_TOKEN) {
  console.error(
    "❌ BOT_TOKEN не найден в Environment Variables"
  );

  process.exit(1);
}

const bot = new Telegraf(BOT_TOKEN);

const PORT =
  Number(process.env.PORT) || 3000;

const REPORT_DIR =
  path.join(
    process.cwd(),
    "reports"
  );

if (!fs.existsSync(REPORT_DIR)) {
  fs.mkdirSync(
    REPORT_DIR,
    {
      recursive: true
    }
  );
}


/*
==========================================================
                    USER STATES
==========================================================
*/

const users = new Map();


/*
==========================================================
             DISPOSABLE EMAIL DOMAINS
==========================================================

Это базовый список.
Его можно расширять без изменения логики.

==========================================================
*/

const DISPOSABLE_DOMAINS = new Set([
  "10minutemail.com",
  "10minutemail.net",
  "10minutemail.org",
  "20minutemail.com",
  "33mail.com",
  "anonaddy.com",
  "burnermail.io",
  "dispostable.com",
  "emailondeck.com",
  "fakeinbox.com",
  "fakemail.net",
  "guerrillamail.com",
  "guerrillamail.net",
  "guerrillamail.org",
  "guerrillamailblock.com",
  "inboxbear.com",
  "mail-temp.com",
  "mail.tm",
  "mailcatch.com",
  "maildrop.cc",
  "mailinator.com",
  "mailnesia.com",
  "mailsac.com",
  "mintemail.com",
  "mytemp.email",
  "sharklasers.com",
  "spam4.me",
  "temp-mail.org",
  "temp-mail.io",
  "tempail.com",
  "tempmail.com",
  "tempmailo.com",
  "throwawaymail.com",
  "trashmail.com",
  "trashmail.net",
  "yopmail.com",
  "yopmail.fr",
  "yopmail.net"
]);


/*
==========================================================
                    WEBMAIL DOMAINS
==========================================================
*/

const WEBMAIL_DOMAINS = new Set([
  "gmail.com",
  "googlemail.com",
  "outlook.com",
  "hotmail.com",
  "live.com",
  "msn.com",
  "yahoo.com",
  "yahoo.co.uk",
  "icloud.com",
  "me.com",
  "mac.com",
  "proton.me",
  "protonmail.com",
  "pm.me",
  "gmx.com",
  "gmx.net",
  "mail.com",
  "zoho.com",
  "yandex.com",
  "yandex.ru",
  "mail.ru",
  "rambler.ru",
  "aol.com"
]);


/*
==========================================================
                    GENERAL HELPERS
==========================================================
*/

function sleep(ms) {
  return new Promise(
    resolve => setTimeout(resolve, ms)
  );
}


function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}


function escapeAttribute(value) {
  return escapeHtml(value);
}


function formatDate(date = new Date()) {
  return new Intl.DateTimeFormat(
    "ru-RU",
    {
      dateStyle: "long",
      timeStyle: "medium",
      timeZone: "Asia/Yerevan"
    }
  ).format(date);
}


function normalizeEmail(email) {
  return String(email)
    .trim()
    .toLowerCase();
}


function getDomain(email) {
  const parts = email.split("@");

  if (parts.length !== 2) {
    return "";
  }

  return parts[1].toLowerCase();
}


function isValidEmail(email) {
  if (!email) {
    return false;
  }

  if (email.length > 320) {
    return false;
  }

  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/i.test(
    email
  );
}


/*
==========================================================
             DOMAIN / EMAIL LOCAL PART
==========================================================
*/

function validateEmailParts(email) {
  const result = {
    valid: true,
    reasons: []
  };

  const parts = email.split("@");

  if (parts.length !== 2) {
    return {
      valid: false,
      reasons: [
        "Email должен содержать один символ @."
      ]
    };
  }

  const local = parts[0];
  const domain = parts[1];

  if (!local) {
    result.valid = false;
    result.reasons.push(
      "Отсутствует имя почтового ящика."
    );
  }

  if (!domain) {
    result.valid = false;
    result.reasons.push(
      "Отсутствует домен."
    );
  }

  if (local.length > 64) {
    result.valid = false;
    result.reasons.push(
      "Локальная часть email слишком длинная."
    );
  }

  if (domain.length > 253) {
    result.valid = false;
    result.reasons.push(
      "Домен слишком длинный."
    );
  }

  if (
    domain.startsWith(".") ||
    domain.endsWith(".") ||
    domain.includes("..")
  ) {
    result.valid = false;
    result.reasons.push(
      "Некорректная структура домена."
    );
  }

  return result;
}


/*
==========================================================
                    DNS HELPERS
==========================================================
*/

async function resolveMx(domain) {
  try {
    const records =
      await dns.resolveMx(domain);

    records.sort(
      (a, b) =>
        Number(a.priority) -
        Number(b.priority)
    );

    return {
      success: true,
      records
    };

  } catch (error) {

    return {
      success: false,
      records: [],
      error: error.message
    };
  }
}


async function resolveA(domain) {
  try {
    const records =
      await dns.resolve4(domain);

    return {
      success: true,
      records
    };

  } catch (error) {

    return {
      success: false,
      records: [],
      error: error.message
    };
  }
}


async function resolveAAAA(domain) {
  try {
    const records =
      await dns.resolve6(domain);

    return {
      success: true,
      records
    };

  } catch (error) {

    return {
      success: false,
      records: [],
      error: error.message
    };
  }
}


async function resolveTxt(domain) {
  try {
    const records =
      await dns.resolveTxt(domain);

    const flattened = records
      .map(row => row.join(""))
      .filter(Boolean);

    return {
      success: true,
      records: flattened
    };

  } catch (error) {

    return {
      success: false,
      records: [],
      error: error.message
    };
  }
}


async function resolveNs(domain) {
  try {
    const records =
      await dns.resolveNs(domain);

    return {
      success: true,
      records
    };

  } catch (error) {

    return {
      success: false,
      records: [],
      error: error.message
    };
  }
}


/*
==========================================================
                    SPF CHECK
==========================================================
*/

async function checkSPF(domain) {
  const result =
    await resolveTxt(domain);

  if (!result.success) {
    return {
      status: "unknown",
      found: false,
      records: [],
      explanation:
        "Не удалось получить TXT-записи домена."
    };
  }

  const records =
    result.records.filter(
      record =>
        /^v=spf1(?:\s|$)/i.test(record)
    );

  if (!records.length) {
    return {
      status: "not_found",
      found: false,
      records: [],
      explanation:
        "SPF-запись не обнаружена."
    };
  }

  return {
    status: "found",
    found: true,
    records,
    explanation:
      "SPF-запись обнаружена."
  };
}


/*
==========================================================
                    DMARC CHECK
==========================================================
*/

async function checkDMARC(domain) {
  const dmarcDomain =
    `_dmarc.${domain}`;

  const result =
    await resolveTxt(dmarcDomain);

  if (!result.success) {
    return {
      status: "not_found",
      found: false,
      records: [],
      explanation:
        "DMARC-запись не обнаружена."
    };
  }

  const records =
    result.records.filter(
      record =>
        /^v=dmarc1(?:\s|;|$)/i.test(record)
    );

  if (!records.length) {
    return {
      status: "not_found",
      found: false,
      records: [],
      explanation:
        "DMARC-запись не обнаружена."
    };
  }

  return {
    status: "found",
    found: true,
    records,
    explanation:
      "DMARC-запись обнаружена."
  };
}


/*
==========================================================
             DISPOSABLE / WEBMAIL CHECK
==========================================================
*/

function checkDomainType(domain) {

  const normalized =
    domain.toLowerCase();

  const disposable =
    DISPOSABLE_DOMAINS.has(
      normalized
    );

  const webmail =
    WEBMAIL_DOMAINS.has(
      normalized
    );

  return {
    disposable,
    webmail
  };
}


/*
==========================================================
                SMTP CONNECTION CHECK
==========================================================

ВАЖНО:

Мы НЕ отправляем RCPT TO и не пытаемся перечислять
существующие почтовые ящики.

Проверяем только:

- доступность MX;
- TCP connection;
- SMTP banner.

Это позволяет определить, доступен ли почтовый
сервер, но НЕ гарантирует существование конкретного
email.

==========================================================
*/

function checkSmtpServer(
  host,
  timeout = 7000
) {

  return new Promise(resolve => {

    let finished = false;
    let socket = null;

    const finish = result => {

      if (finished) {
        return;
      }

      finished = true;

      if (socket) {
        socket.destroy();
      }

      resolve(result);
    };

    socket = net.createConnection({
      host,
      port: 25
    });

    socket.setTimeout(timeout);

    let buffer = "";

    socket.on(
      "connect",
      () => {
        // Соединение установлено.
      }
    );

    socket.on(
      "data",
      data => {

        buffer +=
          data.toString(
            "utf8"
          );

        const firstLine =
          buffer
            .split(/\r?\n/)
            .find(Boolean);

        if (firstLine) {

          const smtpCode =
            firstLine.match(
              /^(\d{3})/
            );

          finish({
            reachable: true,
            banner:
              firstLine.slice(
                0,
                300
              ),
            code:
              smtpCode
                ? smtpCode[1]
                : null
          });
        }
      }
    );

    socket.on(
      "timeout",
      () => {

        finish({
          reachable: false,
          timeout: true,
          banner: "",
          error:
            "SMTP connection timeout"
        });
      }
    );

    socket.on(
      "error",
      error => {

        finish({
          reachable: false,
          banner: "",
          error:
            error.message
        });
      }
    );

    socket.on(
      "close",
      () => {

        if (!finished) {

          finish({
            reachable: false,
            banner: "",
            error:
              "SMTP connection closed"
          });
        }
      }
    );

  });
}


/*
==========================================================
                    SMTP CHECK
==========================================================
*/

async function checkSMTP(mxRecords) {

  if (!mxRecords.length) {

    return {
      status: "not_available",
      reachable: false,
      server: null,
      banner: null,
      error:
        "MX серверы отсутствуют."
    };
  }

  const tested = [];

  /*
  Проверяем максимум 3 MX,
  чтобы не делать десятки соединений.
  */

  const targets =
    mxRecords.slice(0, 3);

  for (
    const mx of targets
  ) {

    const result =
      await checkSmtpServer(
        mx.exchange
      );

    tested.push({
      server:
        mx.exchange,
      priority:
        mx.priority,
      ...result
    });

    if (
      result.reachable
    ) {

      return {
        status: "reachable",
        reachable: true,
        server:
          mx.exchange,
        banner:
          result.banner,
        tested
      };
    }
  }

  return {
    status: "unreachable",
    reachable: false,
    server:
      tested[0]?.server || null,
    banner: null,
    tested,
    error:
      "Не удалось установить SMTP-соединение."
  };
}


/*
==========================================================
                 DNSSEC INDICATOR
==========================================================
*/

async function checkDnssec(domain) {

  try {

    const result =
      await dns.resolveAny(domain);

    const hasDnskey =
      Array.isArray(result) &&
      result.some(
        record =>
          record.type ===
          "DNSKEY"
      );

    return {
      available:
        hasDnskey,
      status:
        hasDnskey
          ? "detected"
          : "not_detected"
    };

  } catch {

    return {
      available: false,
      status: "unknown"
    };
  }
}


/*
==========================================================
              COMPLETE EMAIL ANALYSIS
==========================================================
*/

async function analyzeEmail(
  email
) {

  const startedAt =
    Date.now();

  const domain =
    getDomain(email);

  const localPart =
    email.split("@")[0];

  const result = {

    email,

    localPart,

    domain,

    checkedAt:
      new Date(),

    duration: 0,

    syntax: {
      valid: false,
      reasons: []
    },

    domainDns: {
      exists: false,
      a: [],
      aaaa: [],
      ns: []
    },

    mx: {
      exists: false,
      records: []
    },

    smtp: {
      status: "unknown",
      reachable: false,
      server: null,
      banner: null
    },

    spf: {
      status: "unknown",
      found: false,
      records: []
    },

    dmarc: {
      status: "unknown",
      found: false,
      records: []
    },

    domainType: {
      disposable: false,
      webmail: false
    },

    dnssec: {
      status: "unknown"
    },

    conclusion: {
      status: "unknown",
      title: "",
      explanation: ""
    }
  };


  /*
  ------------------------------------------------------
  SYNTAX
  ------------------------------------------------------
  */

  const syntax =
    validateEmailParts(email);

  result.syntax =
    syntax;


  if (!syntax.valid) {

    result.conclusion = {
      status: "invalid",
      title:
        "❌ Некорректный email",
      explanation:
        syntax.reasons.join(" ")
    };

    result.duration =
      Date.now() - startedAt;

    return result;
  }


  /*
  ------------------------------------------------------
  PARALLEL DNS CHECKS
  ------------------------------------------------------
  */

  const [
    mxResult,
    aResult,
    aaaaResult,
    nsResult,
    spfResult,
    dmarcResult,
    dnssecResult
  ] = await Promise.all([
    resolveMx(domain),
    resolveA(domain),
    resolveAAAA(domain),
    resolveNs(domain),
    checkSPF(domain),
    checkDMARC(domain),
    checkDnssec(domain)
  ]);


  /*
  ------------------------------------------------------
  DOMAIN DNS
  ------------------------------------------------------
  */

  result.domainDns = {
    exists:
      aResult.success ||
      aaaaResult.success ||
      mxResult.success ||
      nsResult.success,

    a:
      aResult.records,

    aaaa:
      aaaaResult.records,

    ns:
      nsResult.records
  };


  /*
  ------------------------------------------------------
  MX
  ------------------------------------------------------
  */

  result.mx = {
    exists:
      mxResult.success &&
      mxResult.records.length > 0,

    records:
      mxResult.records
  };


  /*
  ------------------------------------------------------
  SPF
  ------------------------------------------------------
  */

  result.spf =
    spfResult;


  /*
  ------------------------------------------------------
  DMARC
  ------------------------------------------------------
  */

  result.dmarc =
    dmarcResult;


  /*
  ------------------------------------------------------
  DOMAIN TYPE
  ------------------------------------------------------
  */

  result.domainType =
    checkDomainType(
      domain
    );


  /*
  ------------------------------------------------------
  DNSSEC
  ------------------------------------------------------
  */

  result.dnssec =
    dnssecResult;


  /*
  ------------------------------------------------------
  SMTP
  ------------------------------------------------------
  */

  if (
    result.mx.exists
  ) {

    result.smtp =
      await checkSMTP(
        result.mx.records
      );
  }


  /*
  ------------------------------------------------------
  CONCLUSION
  ------------------------------------------------------
  */

  if (
    !result.domainDns.exists
  ) {

    result.conclusion = {
      status: "invalid",
      title:
        "❌ Домен не найден",
      explanation:
        "DNS не подтвердил существование домена."
    };

  } else if (
    !result.mx.exists
  ) {

    result.conclusion = {
      status: "no_mx",
      title:
        "⚠️ Почтовый сервер не найден",
      explanation:
        "Домен существует, но MX-записи не обнаружены. Это означает, что обычная доставка почты на этот домен не подтверждена."
    };

  } else if (
    result.smtp.reachable
  ) {

    result.conclusion = {
      status: "mail_server",
      title:
        "🟢 Почтовый сервер доступен",
      explanation:
        "Домен существует, MX-записи найдены, и SMTP-сервер отвечает. Это подтверждает работу почтовой инфраструктуры домена, но не доказывает существование конкретного ящика."
    };

  } else {

    result.conclusion = {
      status: "unknown",
      title:
        "🟡 Невозможно определить ящик",
      explanation:
        "Домен и MX-записи существуют, но SMTP-сервер не позволил подтвердить доступность. Существование конкретного почтового ящика определить достоверно невозможно."
    };
  }


  /*
  ------------------------------------------------------
  DISPOSABLE OVERRIDE
  ------------------------------------------------------
  */

  if (
    result.domainType.disposable
  ) {

    result.conclusion = {
      status: "disposable",
      title:
        "🟠 Временный/disposable домен",
      explanation:
        "Домен находится в локальном списке временных почтовых сервисов. Это не доказывает активность конкретного ящика."
    };
  }


  result.duration =
    Date.now() - startedAt;


  return result;
}


/*
==========================================================
                  HTML HELPERS
==========================================================
*/

function statusClass(
  status
) {

  switch (status) {

    case "good":
      return "good";

    case "bad":
      return "bad";

    case "warning":
      return "warning";

    case "info":
      return "info";

    default:
      return "neutral";
  }
}


function statusBadge(
  text,
  status
) {

  return `
    <span class="badge ${statusClass(status)}">
      ${escapeHtml(text)}
    </span>
  `;
}


function yesNo(
  value
) {

  if (value === true) {

    return statusBadge(
      "ДА",
      "good"
    );
  }

  if (value === false) {

    return statusBadge(
      "НЕТ",
      "bad"
    );
  }

  return statusBadge(
    "НЕИЗВЕСТНО",
    "warning"
  );
}


function mxRows(records) {

  if (!records.length) {

    return `
      <tr>
        <td colspan="2">
          MX-записи не найдены
        </td>
      </tr>
    `;
  }

  return records
    .map(
      record => `
        <tr>
          <td>
            ${escapeHtml(
              record.exchange
            )}
          </td>
          <td>
            ${escapeHtml(
              record.priority
            )}
          </td>
        </tr>
      `
    )
    .join("");
}


function listRows(
  records
) {

  if (!records.length) {

    return `
      <div class="muted">
        Нет данных
      </div>
    `;
  }

  return `
    <div class="chips">
      ${records
        .map(
          item =>
            `<span class="chip">
              ${escapeHtml(item)}
            </span>`
        )
        .join("")}
    </div>
  `;
}


/*
==========================================================
                HTML REPORT GENERATOR
==========================================================
*/

function generateHtml(
  result
) {

  const status =
    result.conclusion.status;

  let headerClass =
    "neutral";

  if (
    status === "mail_server"
  ) {
    headerClass =
      "good";
  }

  if (
    status === "invalid" ||
    status === "no_mx"
  ) {
    headerClass =
      "bad";
  }

  if (
    status === "unknown" ||
    status === "disposable"
  ) {
    headerClass =
      "warning";
  }


  const smtpTested =
    result.smtp.tested || [];


  return `<!DOCTYPE html>
<html lang="ru">
<head>

<meta charset="UTF-8">

<meta
  name="viewport"
  content="width=device-width, initial-scale=1.0"
>

<title>
  Email Verification Report
</title>

<style>

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  padding: 0;

  font-family:
    Inter,
    -apple-system,
    BlinkMacSystemFont,
    "Segoe UI",
    Arial,
    sans-serif;

  background:
    #0a0d14;

  color:
    #eef2ff;

  line-height:
    1.55;
}

.container {
  width: 100%;
  max-width: 1000px;

  margin: 0 auto;

  padding: 40px 20px 70px;
}

.header {
  padding: 34px;

  border-radius: 24px;

  background:
    linear-gradient(
      135deg,
      #151b2d,
      #0f1422
    );

  border:
    1px solid #283149;

  box-shadow:
    0 20px 60px
    rgba(0,0,0,.35);

  margin-bottom: 22px;
}

.logo {
  font-size: 14px;

  letter-spacing: 2px;

  text-transform:
    uppercase;

  color:
    #8e9bb8;

  margin-bottom:
    15px;
}

.email {
  font-size:
    clamp(22px, 5vw, 38px);

  font-weight:
    800;

  word-break:
    break-word;

  margin-bottom:
    20px;
}

.result {
  padding:
    18px 20px;

  border-radius:
    16px;

  font-size:
    19px;

  font-weight:
    700;
}

.result.good {
  background:
    rgba(44, 200, 120, .12);

  border:
    1px solid
    rgba(44, 200, 120, .35);

  color:
    #62e6a1;
}

.result.bad {
  background:
    rgba(255, 76, 96, .12);

  border:
    1px solid
    rgba(255, 76, 96, .35);

  color:
    #ff7b8a;
}

.result.warning {
  background:
    rgba(255, 183, 77, .12);

  border:
    1px solid
    rgba(255, 183, 77, .35);

  color:
    #ffc46b;
}

.result.neutral {
  background:
    rgba(130, 150, 190, .12);

  border:
    1px solid
    rgba(130, 150, 190, .3);

  color:
    #b8c4dd;
}

.grid {
  display:
    grid;

  grid-template-columns:
    repeat(
      auto-fit,
      minmax(280px, 1fr)
    );

  gap:
    18px;
}

.card {
  background:
    #111622;

  border:
    1px solid #252e42;

  border-radius:
    20px;

  padding:
    24px;

  margin-bottom:
    18px;

  box-shadow:
    0 12px 35px
    rgba(0,0,0,.18);
}

.card h2 {
  margin:
    0 0 18px;

  font-size:
    18px;
}

.card p {
  color:
    #aab5cb;
}

.row {
  display:
    flex;

  justify-content:
    space-between;

  gap:
    20px;

  padding:
    13px 0;

  border-bottom:
    1px solid #20283a;
}

.row:last-child {
  border-bottom:
    none;
}

.label {
  color:
    #8d9ab3;
}

.value {
  text-align:
    right;

  font-weight:
    600;

  word-break:
    break-word;
}

.badge {
  display:
    inline-block;

  padding:
    5px 10px;

  border-radius:
    999px;

  font-size:
    12px;

  font-weight:
    800;

  letter-spacing:
    .5px;
}

.badge.good {
  background:
    rgba(44,200,120,.12);

  color:
    #62e6a1;
}

.badge.bad {
  background:
    rgba(255,76,96,.12);

  color:
    #ff7b8a;
}

.badge.warning {
  background:
    rgba(255,183,77,.12);

  color:
    #ffc46b;
}

.badge.info {
  background:
    rgba(90,150,255,.12);

  color:
    #7db1ff;
}

.badge.neutral {
  background:
    rgba(140,155,185,.12);

  color:
    #b6c0d5;
}

table {
  width:
    100%;

  border-collapse:
    collapse;
}

th,
td {
  padding:
    13px;

  text-align:
    left;

  border-bottom:
    1px solid #252e42;
}

th {
  color:
    #8d9ab3;

  font-size:
    13px;
}

td {
  word-break:
    break-word;
}

.chips {
  display:
    flex;

  flex-wrap:
    wrap;

  gap:
    8px;
}

.chip {
  padding:
    7px 10px;

  border-radius:
    10px;

  background:
    #1a2130;

  border:
    1px solid #29334a;

  font-size:
    12px;

  color:
    #c4cee0;
}

.muted {
  color:
    #727f97;
}

.explanation {
  padding:
    18px;

  border-radius:
    14px;

  background:
    #0d121d;

  color:
    #adb8cc;
}

.footer {
  margin-top:
    28px;

  text-align:
    center;

  color:
    #66738c;

  font-size:
    12px;
}

code {
  padding:
    3px 7px;

  background:
    #0b1019;

  border-radius:
    6px;

  color:
    #d8e1f5;
}

@media(max-width:600px) {

  .container {
    padding:
      18px 12px 40px;
  }

  .header {
    padding:
      22px;
  }

  .card {
    padding:
      18px;
  }

  .row {
    flex-direction:
      column;

    gap:
      5px;
  }

  .value {
    text-align:
      left;
  }
}

</style>

</head>

<body>

<div class="container">

  <section class="header">

    <div class="logo">
      EMAIL VERIFICATION
    </div>

    <div class="email">
      ${escapeHtml(result.email)}
    </div>

    <div class="result ${headerClass}">
      ${escapeHtml(
        result.conclusion.title
      )}
    </div>

  </section>


  <div class="grid">

    <section class="card">

      <h2>
        📧 Основная информация
      </h2>

      <div class="row">
        <div class="label">
          Email
        </div>

        <div class="value">
          ${escapeHtml(result.email)}
        </div>
      </div>

      <div class="row">
        <div class="label">
          Домен
        </div>

        <div class="value">
          ${escapeHtml(result.domain)}
        </div>
      </div>

      <div class="row">
        <div class="label">
          Локальная часть
        </div>

        <div class="value">
          ${escapeHtml(result.localPart)}
        </div>
      </div>

      <div class="row">
        <div class="label">
          Формат
        </div>

        <div class="value">
          ${yesNo(
            result.syntax.valid
          )}
        </div>
      </div>

    </section>


    <section class="card">

      <h2>
        🌐 Домен
      </h2>

      <div class="row">
        <div class="label">
          Домен существует
        </div>

        <div class="value">
          ${yesNo(
            result.domainDns.exists
          )}
        </div>
      </div>

      <div class="row">
        <div class="label">
          Webmail
        </div>

        <div class="value">
          ${yesNo(
            result.domainType.webmail
          )}
        </div>
      </div>

      <div class="row">
        <div class="label">
          Временный домен
        </div>

        <div class="value">
          ${yesNo(
            result.domainType.disposable
          )}
        </div>
      </div>

      <div class="row">
        <div class="label">
          DNSSEC
        </div>

        <div class="value">
          ${
            result.dnssec.status ===
            "detected"
              ? statusBadge(
                  "ОБНАРУЖЕН",
                  "good"
                )
              : statusBadge(
                  "НЕ ПОДТВЕРЖДЁН",
                  "warning"
                )
          }
        </div>
      </div>

    </section>

  </div>


  <section class="card">

    <h2>
      📬 Почтовая инфраструктура
    </h2>

    <div class="row">
      <div class="label">
        MX-записи
      </div>

      <div class="value">
        ${yesNo(
          result.mx.exists
        )}
      </div>
    </div>

    <div class="row">
      <div class="label">
        Количество MX
      </div>

      <div class="value">
        ${result.mx.records.length}
      </div>
    </div>

    ${
      result.mx.records.length
        ? `
          <table>
            <thead>
              <tr>
                <th>
                  Почтовый сервер
                </th>

                <th>
                  Приоритет
                </th>
              </tr>
            </thead>

            <tbody>
              ${mxRows(
                result.mx.records
              )}
            </tbody>
          </table>
        `
        : ""
    }

  </section>


  <div class="grid">

    <section class="card">

      <h2>
        📡 SMTP
      </h2>

      <div class="row">
        <div class="label">
          Сервер доступен
        </div>

        <div class="value">
          ${
            result.smtp.reachable
              ? statusBadge(
                  "ДА",
                  "good"
                )
              : statusBadge(
                  "НЕТ / НЕИЗВЕСТНО",
                  "warning"
                )
          }
        </div>
      </div>

      <div class="row">
        <div class="label">
          Сервер
        </div>

        <div class="value">
          ${
            result.smtp.server
              ? escapeHtml(
                  result.smtp.server
                )
              : "—"
          }
        </div>
      </div>

      <div class="row">
        <div class="label">
          SMTP banner
        </div>

        <div class="value">
          ${
            result.smtp.banner
              ? escapeHtml(
                  result.smtp.banner
                )
              : "—"
          }
        </div>
      </div>

      <p>
        SMTP-соединение показывает доступность
        почтового сервера. Оно не является
        гарантией существования конкретного
        почтового ящика.
      </p>

    </section>


    <section class="card">

      <h2>
        🛡️ Безопасность домена
      </h2>

      <div class="row">
        <div class="label">
          SPF
        </div>

        <div class="value">
          ${
            result.spf.found
              ? statusBadge(
                  "НАЙДЕН",
                  "good"
                )
              : statusBadge(
                  "НЕ НАЙДЕН",
                  "warning"
                )
          }
        </div>
      </div>

      <div class="row">
        <div class="label">
          DMARC
        </div>

        <div class="value">
          ${
            result.dmarc.found
              ? statusBadge(
                  "НАЙДЕН",
                  "good"
                )
              : statusBadge(
                  "НЕ НАЙДЕН",
                  "warning"
                )
          }
        </div>
      </div>

    </section>

  </div>


  <section class="card">

    <h2>
      🧩 DNS-информация
    </h2>

    <h3>
      A
    </h3>

    ${listRows(
      result.domainDns.a
    )}

    <h3>
      AAAA
    </h3>

    ${listRows(
      result.domainDns.aaaa
    )}

    <h3>
      NS
    </h3>

    ${listRows(
      result.domainDns.ns
    )}

  </section>


  ${
    result.spf.records.length
      ? `
        <section class="card">

          <h2>
            SPF-записи
          </h2>

          ${listRows(
            result.spf.records
          )}

        </section>
      `
      : ""
  }


  ${
    result.dmarc.records.length
      ? `
        <section class="card">

          <h2>
            DMARC-записи
          </h2>

          ${listRows(
            result.dmarc.records
          )}

        </section>
      `
      : ""
  }


  <section class="card">

    <h2>
      🔬 SMTP-сервера, которые проверялись
    </h2>

    ${
      smtpTested.length
        ? `
          <table>

            <thead>

              <tr>
                <th>
                  Server
                </th>

                <th>
                  Priority
                </th>

                <th>
                  Result
                </th>
              </tr>

            </thead>

            <tbody>

              ${smtpTested
                .map(
                  item => `
                    <tr>

                      <td>
                        ${escapeHtml(
                          item.server
                        )}
                      </td>

                      <td>
                        ${escapeHtml(
                          item.priority
                        )}
                      </td>

                      <td>
                        ${
                          item.reachable
                            ? statusBadge(
                                "ДОСТУПЕН",
                                "good"
                              )
                            : statusBadge(
                                "НЕ ДОСТУПЕН",
                                "warning"
                              )
                        }
                      </td>

                    </tr>
                  `
                )
                .join("")}

            </tbody>

          </table>
        `
        : `
          <div class="muted">
            SMTP-серверы для проверки отсутствуют.
          </div>
        `
    }

  </section>


  <section class="card">

    <h2>
      📝 Объяснение результата
    </h2>

    <div class="explanation">

      ${escapeHtml(
        result.conclusion.explanation
      )}

    </div>

    <p>

      <strong>
        Важно:
      </strong>

      наличие MX и ответ SMTP означает,
      что почтовая инфраструктура домена
      доступна. Это не является доказательством
      того, что конкретный адрес существует,
      активен или принадлежит определённому человеку.

    </p>

  </section>


  <section class="card">

    <h2>
      ⏱️ Информация о проверке
    </h2>

    <div class="row">

      <div class="label">
        Время проверки
      </div>

      <div class="value">
        ${escapeHtml(
          formatDate(
            result.checkedAt
          )
        )}
      </div>

    </div>

    <div class="row">

      <div class="label">
        Время выполнения
      </div>

      <div class="value">
        ${result.duration} ms
      </div>

    </div>

  </section>


  <div class="footer">

    Email Verification Report<br>

    Automated technical analysis

  </div>

</div>

</body>
</html>`;
}


/*
==========================================================
              SAVE HTML REPORT
==========================================================
*/

function saveReport(
  result
) {

  const safeEmail =
    result.email
      .replace(
        /[^a-z0-9._-]/gi,
        "_"
      );

  const timestamp =
    Date.now();

  const filename =
    `email-report-${safeEmail}-${timestamp}.html`;

  const filepath =
    path.join(
      REPORT_DIR,
      filename
    );

  const html =
    generateHtml(
      result
    );

  fs.writeFileSync(
    filepath,
    html,
    "utf8"
  );

  return {
    filepath,
    filename,
    html
  };
}


/*
==========================================================
                 MAIN MENU
==========================================================
*/

function mainMenu() {

  return Markup.inlineKeyboard([
    [
      Markup.button.callback(
        "🔎 Проверить email",
        "probit_email"
      )
    ]
  ]);
}


/*
==========================================================
                     /START
==========================================================
*/

bot.start(
  async ctx => {

    users.set(
      ctx.chat.id,
      {
        step: "start"
      }
    );

    await ctx.reply(
      "📧 Email Verification\n\n" +
      "Введите email, и я проведу техническую " +
      "проверку домена и почтовой инфраструктуры.",
      mainMenu()
    );
  }
);


/*
==========================================================
                START CHECK
==========================================================
*/

bot.action(
  "probit_email",
  async ctx => {

    await ctx
      .answerCbQuery()
      .catch(() => {});

    users.set(
      ctx.chat.id,
      {
        step: "await_email"
      }
    );

    await ctx.reply(
      "📧 Введите email для проверки:"
    );
  }
);


/*
==========================================================
                   CANCEL
==========================================================
*/

bot.action(
  "cancel",
  async ctx => {

    await ctx
      .answerCbQuery()
      .catch(() => {});

    users.set(
      ctx.chat.id,
      {
        step: "start"
      }
    );

    await ctx.reply(
      "Отменено.",
      mainMenu()
    );
  }
);


/*
==========================================================
                 TEXT HANDLER
==========================================================
*/

bot.on(
  "text",
  async ctx => {

    const chatId =
      ctx.chat.id;

    const state =
      users.get(chatId);

    if (!state) {
      return;
    }

    if (
      state.step !==
      "await_email"
    ) {
      return;
    }

    const email =
      normalizeEmail(
        ctx.message.text
      );


    /*
    ------------------------------------------------------
    BASIC FORMAT
    ------------------------------------------------------
    */

    if (!isValidEmail(email)) {

      await ctx.reply(
        "❌ Некорректный email.\n\n" +
        "Пример:\n" +
        "example@gmail.com\n\n" +
        "Попробуйте ещё раз."
      );

      return;
    }


    /*
    ------------------------------------------------------
    SEARCHING STATE
    ------------------------------------------------------
    */

    users.set(
      chatId,
      {
        step: "searching"
      }
    );


    /*
    ------------------------------------------------------
    SEARCH MESSAGE
    ------------------------------------------------------
    */

    const searchingMessage =
      await ctx.reply(
        "🔎 Начинаю техническую проверку...\n\n" +
        "• Проверяю формат\n" +
        "• Проверяю DNS\n" +
        "• Ищу MX\n" +
        "• Проверяю SMTP\n" +
        "• Проверяю SPF\n" +
        "• Проверяю DMARC\n" +
        "• Определяю тип домена"
      );


    try {

      /*
      ----------------------------------------------------
      ANALYSIS
      ----------------------------------------------------
      */

      const result =
        await analyzeEmail(
          email
        );


      /*
      ----------------------------------------------------
      UPDATE SEARCH MESSAGE
      ----------------------------------------------------
      */

      try {

        await ctx.telegram.editMessageText(
          ctx.chat.id,
          searchingMessage.message_id,
          undefined,
          "✅ Проверка завершена.\n\n" +
          "Формирую HTML-отчёт..."
        );

      } catch {}


      /*
      ----------------------------------------------------
      GENERATE REPORT
      ----------------------------------------------------
      */

      const report =
        saveReport(
          result
        );


      /*
      ----------------------------------------------------
      SHORT RESULT IN TELEGRAM
      ----------------------------------------------------
      */

      let shortStatus =
        "🟡 Не удалось определить";

      if (
        result.conclusion.status ===
        "mail_server"
      ) {

        shortStatus =
          "🟢 Почтовый сервер доступен";
      }

      if (
        result.conclusion.status ===
        "no_mx"
      ) {

        shortStatus =
          "🟠 MX не найден";
      }

      if (
        result.conclusion.status ===
        "invalid"
      ) {

        shortStatus =
          "🔴 Некорректный email";
      }

      if (
        result.conclusion.status ===
        "disposable"
      ) {

        shortStatus =
          "🟠 Disposable email";
      }


      const summary =
        "📊 <b>Проверка завершена</b>\n\n" +

        "📧 Email: <code>" +
        escapeHtml(email) +
        "</code>\n\n" +

        "Статус: <b>" +
        escapeHtml(shortStatus) +
        "</b>\n\n" +

        "🌐 Домен: " +
        (
          result.domainDns.exists
            ? "✅ существует"
            : "❌ не найден"
        ) +
        "\n" +

        "📬 MX: " +
        (
          result.mx.exists
            ? "✅ найден"
            : "❌ не найден"
        ) +
        "\n" +

        "📡 SMTP: " +
        (
          result.smtp.reachable
            ? "✅ сервер отвечает"
            : "⚪ не подтверждён"
        ) +
        "\n" +

        "🛡 SPF: " +
        (
          result.spf.found
            ? "✅"
            : "⚪"
        ) +
        "\n" +

        "🛡 DMARC: " +
        (
          result.dmarc.found
            ? "✅"
            : "⚪"
        ) +
        "\n\n" +

        "⚠️ Конкретный почтовый ящик " +
        "не считается существующим только " +
        "на основании MX/SMTP.";

      await ctx.reply(
        summary,
        {
          parse_mode: "HTML",
          ...Markup.inlineKeyboard([
            [
              Markup.button.callback(
                "🔄 Новый поиск",
                "probit_email"
              )
            ]
          ])
        }
      );


      /*
      ----------------------------------------------------
      SEND HTML FILE
      ----------------------------------------------------
      */

      await ctx.replyWithDocument(
        {
          source:
            report.filepath
        },
        {
          caption:
            "📄 Полный HTML-отчёт\n\n" +
            "Откройте файл в браузере, чтобы " +
            "посмотреть подробную информацию."
        }
      );


    } catch (error) {

      console.error(
        "❌ Ошибка проверки:",
        error
      );

      await ctx.reply(
        "❌ Во время проверки произошла ошибка.\n\n" +
        "Попробуйте другой email."
      );

    } finally {

      users.set(
        chatId,
        {
          step: "start"
        }
      );
    }

  }
);


/*
==========================================================
                   TELEGRAM ERRORS
==========================================================
*/

bot.catch(
  (error, ctx) => {

    console.error(
      "Telegram error:",
      error?.message ||
      error
    );

  }
);


/*
==========================================================
                    HTTP SERVER
==========================================================
*/

const server =
  http.createServer(
    (req, res) => {

      if (
        req.url === "/" ||
        req.url === "/health"
      ) {

        res.writeHead(
          200,
          {
            "Content-Type":
              "text/plain; charset=utf-8"
          }
        );

        res.end(
          "Email verification bot is running"
        );

        return;
      }


      if (
        req.url === "/status"
      ) {

        const status = {

          status:
            "online",

          service:
            "email-verification-bot",

          uptime:
            process.uptime(),

          time:
            new Date().toISOString(),

          users:
            users.size

        };


        res.writeHead(
          200,
          {
            "Content-Type":
              "application/json; charset=utf-8"
          }
        );

        res.end(
          JSON.stringify(
            status,
            null,
            2
          )
        );

        return;
      }


      res.writeHead(
        404,
        {
          "Content-Type":
            "text/plain; charset=utf-8"
        }
      );

      res.end(
        "Not Found"
      );

    }
  );


/*
==========================================================
                     RENDER
==========================================================
*/

server.listen(
  PORT,
  "0.0.0.0",
  () => {

    console.log(
      `🌐 HTTP server started on port ${PORT}`
    );

  }
);


/*
==========================================================
                 START TELEGRAM BOT
==========================================================
*/

(async () => {

  try {

    console.log(
      "🚀 Starting Telegram bot..."
    );


    /*
    Очищаем старые pending updates.
    */

    await bot.launch({
      dropPendingUpdates: true
    });


    console.log(
      "✅ Telegram bot started successfully"
    );


  } catch (error) {

    console.error(
      "❌ Telegram bot startup error:"
    );

    console.error(
      error
    );

    process.exit(1);
  }

})();


/*
==========================================================
                     SHUTDOWN
==========================================================
*/

process.once(
  "SIGINT",
  () => {

    console.log(
      "Stopping bot..."
    );

    bot.stop(
      "SIGINT"
    );

    server.close();

  }
);


process.once(
  "SIGTERM",
  () => {

    console.log(
      "Stopping bot..."
    );

    bot.stop(
      "SIGTERM"
    );

    server.close();

  }
);
