const chatEl = document.getElementById("chat");
const formEl = document.getElementById("chatForm");
const inputEl = document.getElementById("messageInput");
const typingEl = document.getElementById("typingIndicator");
const statusLineEl = document.getElementById("statusLine");

const USER_ID_KEY = "cozy_ai_user_id";
const LAST_USER_ACTIVITY_KEY = "cozy_last_user_activity_at";
const CHAT_SEEN_MESSAGE_IDS_KEY = "cozy_seen_message_ids";

let lastUserActivityAt = Number(localStorage.getItem(LAST_USER_ACTIVITY_KEY) || "0");
let seenMessageIds = new Set(JSON.parse(localStorage.getItem(CHAT_SEEN_MESSAGE_IDS_KEY) || "[]"));

const petNames = ["котик", "солнышко", "милый", "родной", "радость моя"];
const smiles = ["(｡♥‿♥｡)", "(⁠*⁠^⁠_⁠^⁠*⁠)", "🤍", "✨"];
const aiOpeners = [
  "Я рядом",
  "Слушаю тебя",
  "Я тут",
  "Поймала твой вайб",
  "Окей, давай мягко разберем",
];
const aiClosers = [
  "если хочешь, я рядом еще чуть-чуть",
  "и да, можешь не держать это в себе",
  "мне важно, как ты",
  "не пропадай, ладно?",
  "я тебя аккуратно обниму словами",
];

function getOrCreateUserId() {
  const existing = localStorage.getItem(USER_ID_KEY);
  if (existing) return existing;

  const newId = `user-${crypto.randomUUID()}`;
  localStorage.setItem(USER_ID_KEY, newId);
  return newId;
}

function persistSeenMessageIds() {
  localStorage.setItem(CHAT_SEEN_MESSAGE_IDS_KEY, JSON.stringify([...seenMessageIds]));
}

function persistLastUserActivity() {
  localStorage.setItem(LAST_USER_ACTIVITY_KEY, String(lastUserActivityAt));
}

function setLastUserActivityNow() {
  lastUserActivityAt = Date.now();
  persistLastUserActivity();
  updateStatusLine();
}

function formatLastSeen(deltaMs) {
  const seconds = Math.max(0, Math.floor(deltaMs / 1000));
  if (seconds < 20) return "онлайн";
  if (seconds < 60) return "была только что";

  const minutes = Math.floor(seconds / 60);
  if (minutes === 1) return "была минуту назад";
  if (minutes < 60) return `была ${minutes} мин назад`;

  const hours = Math.floor(minutes / 60);
  if (hours === 1) return "была час назад";
  return `была ${hours} ч назад`;
}

function updateStatusLine() {
  if (!statusLineEl) return;

  if (!lastUserActivityAt) {
    statusLineEl.textContent = "онлайн";
    return;
  }

  const delta = Date.now() - lastUserActivityAt;
  statusLineEl.textContent = formatLastSeen(delta);
}

function chooseRandom(list) {
  return list[Math.floor(Math.random() * list.length)];
}

function addBubble(text, role) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;
  chatEl.appendChild(bubble);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function showTyping(show) {
  typingEl.classList.toggle("hidden", !show);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function calcTypingDelay(text) {
  const trimmed = text.trim();
  if (trimmed.length <= 10) return 0;
  if (trimmed.length <= 18) return 180;

  const punctuationCount = (trimmed.match(/[,.!?…]/g) || []).length;
  const emotionalCount = (trimmed.match(/[♥🤍✨!]/g) || []).length;
  const longWordsCount = (trimmed.match(/\b\S{8,}\b/g) || []).length;

  const base = 220 + trimmed.length * 18;
  const complexityBoost = punctuationCount * 65 + longWordsCount * 45;
  const emotionalBoost = emotionalCount * 90;

  const total = base + complexityBoost + emotionalBoost;
  return Math.max(140, Math.min(total, 4200));
}

async function showAiMessagesWithTyping(messages) {
  const count = messages.length;

  for (let index = 0; index < count; index += 1) {
    const msg = messages[index];
    const delay = calcTypingDelay(msg) + (index > 0 ? 120 + Math.random() * 180 : 0);
    const shouldShowTyping = delay > 0;

    if (shouldShowTyping) {
      showTyping(true);
      await sleep(delay);
      showTyping(false);
    }

    addBubble(msg, "ai");
    await sleep(80 + Math.random() * 110);
  }
}

function diversifyMessages(messages) {
  const clean = messages
    .map((msg) => String(msg || "").trim())
    .filter(Boolean);

  if (clean.length === 0) {
    return ["Я тут, котик 🤍"];
  }

  if (clean.length === 1) return clean;

  if (clean.length === 2 && Math.random() < 0.45) {
    return [clean[0], `${clean[1]} ${chooseRandom(aiClosers)}`.trim()];
  }

  return clean;
}

async function sendMessage(text) {
  const userId = getOrCreateUserId();

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, user_id: userId }),
  });

  const data = await response.json();

  if (!response.ok) {
    const fallback = (data.messages && data.messages[0]) || "Ошибка сервера";
    await showAiMessagesWithTyping([fallback]);
    return;
  }

  const aiMessages = diversifyMessages(Array.isArray(data.messages) ? data.messages : ["Я тут 🤍"]);
  await showAiMessagesWithTyping(aiMessages);
}

async function loadHistory() {
  const userId = getOrCreateUserId();
  const response = await fetch(`/api/history?user_id=${encodeURIComponent(userId)}`);
  if (!response.ok) return;

  const data = await response.json();
  const messages = Array.isArray(data.messages) ? data.messages : [];

  chatEl.innerHTML = "";
  seenMessageIds = new Set();

  for (const message of messages) {
    if (!message || !message.id) continue;
    seenMessageIds.add(message.id);
    addBubble(message.content, message.role === "user" ? "user" : "ai");
  }

  persistSeenMessageIds();
  if (messages.length > 0 && data.last_user_created_at) {
    const parsed = Date.parse(data.last_user_created_at);
    if (!Number.isNaN(parsed)) {
      lastUserActivityAt = parsed;
      persistLastUserActivity();
    }
  }

  updateStatusLine();
}

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;

  addBubble(text, "user");
  inputEl.value = "";
  setLastUserActivityNow();

  try {
    await sendMessage(text);
  } catch (error) {
    await showAiMessagesWithTyping(["Ой, у меня сбой связи... но я уже снова рядом ✨"]);
  }
});

function iosKeyboardFix() {
  const vv = window.visualViewport;
  if (!vv) return;

  const shell = document.querySelector(".app-shell");
  const apply = () => {
    const viewportHeight = vv.height;
    shell.style.height = `${viewportHeight}px`;
  };

  vv.addEventListener("resize", apply);
  vv.addEventListener("scroll", apply);
  apply();
}

iosKeyboardFix();

updateStatusLine();
setInterval(updateStatusLine, 15000);

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/static/sw.js").catch(() => {});
  });
}

loadHistory().then(() => {
  if (!chatEl.children.length) {
    addBubble(`${chooseRandom(aiOpeners)}, я уже скучала ${chooseRandom(smiles)}`, "ai");
  }
});
