const chatEl = document.getElementById("chat");
const formEl = document.getElementById("chatForm");
const inputEl = document.getElementById("messageInput");
const typingEl = document.getElementById("typingIndicator");

const USER_ID_KEY = "cozy_ai_user_id";

function getOrCreateUserId() {
  const existing = localStorage.getItem(USER_ID_KEY);
  if (existing) return existing;

  const newId = `user-${crypto.randomUUID()}`;
  localStorage.setItem(USER_ID_KEY, newId);
  return newId;
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
  if (trimmed.length <= 12) return 220;

  const punctuationCount = (trimmed.match(/[,.!?…]/g) || []).length;
  const emotionalCount = (trimmed.match(/[♥🤍✨!]/g) || []).length;
  const longWordsCount = (trimmed.match(/\b\S{8,}\b/g) || []).length;

  const base = 380 + trimmed.length * 22;
  const complexityBoost = punctuationCount * 65 + longWordsCount * 45;
  const emotionalBoost = emotionalCount * 90;

  const total = base + complexityBoost + emotionalBoost;
  return Math.max(250, Math.min(total, 5200));
}

async function showAiMessagesWithTyping(messages) {
  for (const msg of messages) {
    showTyping(true);
    await sleep(calcTypingDelay(msg));
    showTyping(false);
    addBubble(msg, "ai");
    await sleep(120);
  }
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

  const aiMessages = Array.isArray(data.messages) ? data.messages : ["Я тут 🤍"];
  await showAiMessagesWithTyping(aiMessages);
}

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;

  addBubble(text, "user");
  inputEl.value = "";

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

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/static/sw.js").catch(() => {});
  });
}

addBubble("Привет, котик... я уже скучала (｡♥‿♥｡)", "ai");
