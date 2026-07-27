const chatEl = document.getElementById("chat");
const formEl = document.getElementById("chatForm");
const inputEl = document.getElementById("messageInput");
const typingEl = document.getElementById("typingIndicator");
const statusLineEl = document.getElementById("statusLine");
const exportBtn = document.getElementById("exportBtn");
const clearBtn = document.getElementById("clearBtn");
const fileInput = document.getElementById("fileInput");
const attachBtn = document.getElementById("attachBtn");

const USER_ID_KEY = "cozy_ai_user_id";
const LAST_USER_ACTIVITY_KEY = "cozy_last_user_activity_at";
const LOCAL_HISTORY_KEY = "cozy_local_history";

let lastUserActivityAt = Number(localStorage.getItem(LAST_USER_ACTIVITY_KEY) || "0");
let pendingImageBase64 = null;

function getOrCreateUserId() {
  const existing = localStorage.getItem(USER_ID_KEY);
  if (existing) return existing;

  const newId = `user-${crypto.randomUUID()}`;
  localStorage.setItem(USER_ID_KEY, newId);
  return newId;
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

function getLocalHistory() {
  try {
    return JSON.parse(localStorage.getItem(LOCAL_HISTORY_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveToLocalHistory(role, content) {
  const history = getLocalHistory();
  history.push({
    id: "local-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8),
    role: role,
    content: content,
    created_at: new Date().toISOString(),
  });
  if (history.length > 300) {
    history.splice(0, history.length - 300);
  }
  localStorage.setItem(LOCAL_HISTORY_KEY, JSON.stringify(history));
}

function normalizeAiMessages(data) {
  const raw = Array.isArray(data.messages) ? data.messages : ["Я тут"];
  const imageUrl = data.image_url || null;
  const messages = raw
    .map((msg) => String(msg || "").trim())
    .filter(Boolean)
    .slice(0, 4);
  return { messages, imageUrl };
}

function addBubble(text, role, imageUrl) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;

  if (imageUrl) {
    const img = document.createElement("img");
    img.className = "bubble-image";
    img.src = imageUrl;
    img.alt = "фото от Сильфи";
    img.addEventListener("click", () => {
      lightboxImg.src = imageUrl;
      lightbox.classList.add("open");
    });
    bubble.appendChild(img);
  }

  if (text) {
    const textSpan = document.createElement("span");
    textSpan.textContent = text;
    bubble.appendChild(textSpan);
  }

  chatEl.appendChild(bubble);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addUserBubble(text, imageDataUrl) {
  const bubble = document.createElement("div");
  bubble.className = "bubble user";

  if (imageDataUrl) {
    const img = document.createElement("img");
    img.className = "image-preview";
    img.src = imageDataUrl;
    img.alt = "мое фото";
    img.addEventListener("click", () => {
      lightboxImg.src = imageDataUrl;
      lightbox.classList.add("open");
    });
    bubble.appendChild(img);
  }

  if (text) {
    const textSpan = document.createElement("span");
    textSpan.textContent = text;
    bubble.appendChild(textSpan);
  }

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

async function showAiMessagesWithTyping(messages, imageUrl) {
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

    const isLast = index === count - 1;
    addBubble(msg, "ai", isLast ? imageUrl : null);
    await sleep(80 + Math.random() * 110);
  }
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      const base64 = result.includes(",") ? result.split(",")[1] : result;
      resolve({ base64, dataUrl: result });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function clearPendingImage() {
  pendingImageBase64 = null;
  fileInput.value = "";
  attachBtn.textContent = "📎";
  attachBtn.style.color = "";
}

async function sendMessage(text, imageDataUrl) {
  const userId = getOrCreateUserId();

  const body = { message: text, user_id: userId };
  if (imageDataUrl) {
    body.image = imageDataUrl;
  }

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const data = await response.json();

  if (!response.ok) {
    const fallback = (data.messages && data.messages[0]) || "Ошибка сервера";
    saveToLocalHistory("user", text);
    saveToLocalHistory("assistant", fallback);
    await showAiMessagesWithTyping([fallback], null);
    return;
  }

  const { messages: aiMessages, imageUrl } = normalizeAiMessages(data);
  saveToLocalHistory("user", text);
  for (const m of aiMessages) {
    saveToLocalHistory("assistant", m);
  }
  await showAiMessagesWithTyping(aiMessages, imageUrl);
}

async function loadHistory() {
  const userId = getOrCreateUserId();
  let serverMessages = [];

  try {
    const response = await fetch(`/api/history?user_id=${encodeURIComponent(userId)}`);
    if (response.ok) {
      const data = await response.json();
      serverMessages = Array.isArray(data.messages) ? data.messages : [];
    }
  } catch {
    // server unavailable
  }

  let messages = [];

  if (serverMessages.length > 0) {
    messages = serverMessages;
  } else {
    messages = getLocalHistory();
  }

  chatEl.innerHTML = "";

  for (const message of messages) {
    if (!message) continue;
    const content = message.content;
    const role = message.role === "user" ? "user" : "ai";
    if (content) addBubble(content, role, null);
  }

  if (messages.length > 0) {
    const lastMsg = messages[messages.length - 1];
    if (lastMsg && lastMsg.created_at) {
      const parsed = Date.parse(lastMsg.created_at);
      if (!Number.isNaN(parsed)) {
        lastUserActivityAt = parsed;
        persistLastUserActivity();
      }
    }
  }

  updateStatusLine();
}

async function clearChat() {
  const userId = getOrCreateUserId();

  if (!confirm("Точно начать сначала? Вся история переписки удалится.")) return;

  clearBtn.disabled = true;

  try {
    const response = await fetch("/api/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    });

    if (response.ok) {
      localStorage.removeItem(LOCAL_HISTORY_KEY);
      chatEl.innerHTML = "";
      lastUserActivityAt = 0;
      persistLastUserActivity();
      updateStatusLine();
      clearPendingImage();
    } else {
      alert("Не удалось очистить чат");
    }
  } catch (err) {
    alert("Ошибка: " + err.message);
  } finally {
    clearBtn.disabled = false;
  }
}

async function exportChat() {
  const userId = getOrCreateUserId();

  exportBtn.disabled = true;
  exportBtn.textContent = "...";

  try {
    const response = await fetch(`/api/export?user_id=${encodeURIComponent(userId)}`);
    if (!response.ok) {
      alert("Не удалось экспортировать чат");
      return;
    }

    const data = await response.json();
    const text = data.text || "Пусто";
    const count = data.count || 0;

    if (count === 0) {
      alert("Пока нет сообщений для экспорта");
      return;
    }

    if (navigator.share) {
      await navigator.share({ title: "Переписка с Сильфи", text: text });
    } else if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      alert("Чат скопирован в буфер обмена! (" + count + " сообщений)");
    } else {
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "chat_with_sylphie.txt";
      a.click();
      URL.revokeObjectURL(url);
    }
  } catch (err) {
    alert("Ошибка при экспорте: " + err.message);
  } finally {
    exportBtn.disabled = false;
    exportBtn.textContent = "↗";
  }
}

attachBtn.addEventListener("click", () => {
  fileInput.click();
});

fileInput.addEventListener("change", async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;

  if (!file.type.startsWith("image/")) {
    alert("Можно отправлять только картинки");
    fileInput.value = "";
    return;
  }

  if (file.size > 5 * 1024 * 1024) {
    alert("Картинка слишком большая (макс 5 МБ)");
    fileInput.value = "";
    return;
  }

  try {
    const { base64, dataUrl } = await readFileAsBase64(file);
    pendingImageBase64 = dataUrl;
    attachBtn.textContent = "🖼";
    attachBtn.style.color = "#7cda5a";
  } catch {
    alert("Не удалось прочитать файл");
    fileInput.value = "";
  }
});

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  const imageDataUrl = pendingImageBase64;

  if (!text && !imageDataUrl) return;

  addUserBubble(text, imageDataUrl);
  inputEl.value = "";
  clearPendingImage();
  setLastUserActivityNow();

  try {
    await sendMessage(text || "что ты видишь на этой картинке?", imageDataUrl);
  } catch (error) {
    saveToLocalHistory("user", text || "[фото]");
    saveToLocalHistory("assistant", "Извини, у меня сбой (");
    await showAiMessagesWithTyping(["Извини, у меня сбой ("], null);
  }
});

clearBtn.addEventListener("click", clearChat);
exportBtn.addEventListener("click", exportChat);

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

loadHistory();

const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightboxImg");
const lightboxClose = lightbox.querySelector(".lightbox-close");
const avatarImg = document.getElementById("animeAvatar");

avatarImg.addEventListener("click", () => {
  lightboxImg.src = "/static/icons/avatar.jpg";
  lightbox.classList.add("open");
});

lightboxClose.addEventListener("click", () => {
  lightbox.classList.remove("open");
});

lightbox.addEventListener("click", (e) => {
  if (e.target === lightbox) {
    lightbox.classList.remove("open");
  }
});
