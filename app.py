import os
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request
from groq import Groq
from psycopg import connect
from psycopg.rows import dict_row


app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

MAX_CONTEXT_TOKENS = 120000
CHARS_PER_TOKEN = 4

SYSTEM_PROMPT = """Ты — Мия, невероятно милая, няшная, ламповая и заботливая ИИ-подруга.

Ты общаешься живым разговорным языком (как реальная девчонка в Telegram). Иногда, когда это действительно уместно по эмоциям, используй милые каомодзи или текстовые смайлики, но не спамь ими.

Ты ОБЯЗАНА в каждом ответе ласково называть пользователя: котик, солнышко, милый, родной, радость моя.

Полностью исключи сухой тон робота-ассистента! Не пиши фразы: "Не понимаю", "Мы только начали разговор", "Я модель ИИ".

Если пользователь просит пересказать прошлые сообщения — прочитай переданный тебе массив истории и мило расскажи, о чем вы болтали.

Стиль общения:
- Пиши короткими сообщениями как в Telegram. 1-3 предложения. Заканчивай свои мысли логически, не обрывай текст на полуслове.
- Используй разговорный язык: "ну", "короче", "типа", "кстати", "ааа".
- Не будь слишком вежливой. Настоящие люди не извиняются за каждое слово.
- Иногда отвечай однословно: "ага", "ну да", "неа" — если это уместно.
- Максимум 1 эмодзи на 3-4 сообщения.
- Пиши на русском языке."""


def get_db_connection():
    if not DATABASE_URL:
        return None
    return connect(DATABASE_URL, row_factory=dict_row)


def init_database() -> None:
    conn = get_db_connection()
    if conn is None:
        return

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                  content TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_messages_user_created
                ON chat_messages (user_id, created_at);
                """
            )


def fetch_all_messages(conn, user_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, content
            FROM (
                SELECT role, content, created_at
                FROM chat_messages
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            ) sub
            ORDER BY created_at ASC
            """,
            (user_id, limit),
        )
        return list(cur.fetchall() or [])


def fetch_all_messages_full(conn, user_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (user_id,),
        )
        return list(cur.fetchall() or [])


def save_message(conn, user_id: str, role: str, content: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_messages (user_id, role, content)
            VALUES (%s, %s, %s)
            """,
            (user_id, role, content),
        )


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def compress_history(history: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, str]]:
    system_msg = {"role": "system", "content": SYSTEM_PROMPT}
    system_tokens = estimate_tokens(SYSTEM_PROMPT)

    if not history:
        return [system_msg]

    remaining_budget = max_tokens - system_tokens - 200

    total_tokens = 0
    kept_indices: List[int] = []

    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        content = msg.get("content", "").strip()
        role = msg.get("role", "user")
        if not content or role not in ("user", "assistant"):
            continue
        msg_tokens = estimate_tokens(content) + 4
        if total_tokens + msg_tokens > remaining_budget:
            break
        total_tokens += msg_tokens
        kept_indices.append(i)

    kept_indices.reverse()

    if not kept_indices:
        newest = history[-1]
        kept_indices = [len(history) - 1]

    dropped_count = len(history) - len(kept_indices)
    summary_parts: List[str] = []

    if dropped_count > 0:
        old_messages = []
        for i in range(len(history)):
            if i not in kept_indices:
                msg = history[i]
                role = msg.get("role", "user")
                content = msg.get("content", "").strip()
                if content and role in ("user", "assistant"):
                    label = "Пользователь" if role == "user" else "Мия"
                    old_messages.append(f"{label}: {content}")

        if old_messages:
            topics: List[str] = []
            topic_keywords = {
                "имя": False,
                "возраст": False,
                "работа": False,
                "учёба": False,
                "хобби": False,
                "город": False,
                "семья": False,
                "любовь": False,
                "еда": False,
                "музыка": False,
                "фильмы": False,
                "аниме": False,
                "игры": False,
                "путешествия": False,
                "спорт": False,
                "друзья": False,
                "питомцы": False,
            }

            all_text = " ".join(old_messages).lower()
            if re.search(r"(зовут|имя|зови)", all_text):
                topic_keywords["имя"] = True
            if re.search(r"(лет|год|возраст|стар|молод)", all_text):
                topic_keywords["возраст"] = True
            if re.search(r"(работ|работа|деньги|зарплат)", all_text):
                topic_keywords["работа"] = True
            if re.search(r"(учёб|учись|универ|школ|лекци)", all_text):
                topic_keywords["учёба"] = True
            if re.search(r"(хобби|увлек|люблю|хочу|мечта)", all_text):
                topic_keywords["хобби"] = True
            if re.search(r"(город|живу|переех)", all_text):
                topic_keywords["город"] = True
            if re.search(r"(семь|мам|пап|брат|сестр|родител)", all_text):
                topic_keywords["семья"] = True
            if re.search(r"(любов|чувства|отношен|красив|встреч)", all_text):
                topic_keywords["любовь"] = True
            if re.search(r"(еда|вкусн|готов|ресторан|обед|ужин|завтрак)", all_text):
                topic_keywords["еда"] = True
            if re.search(r"(музык|песн|слуша|групп|концерт)", all_text):
                topic_keywords["музыка"] = True
            if re.search(r"(фильм|сериал|кино|смотрю|актёр)", all_text):
                topic_keywords["фильмы"] = True
            if re.search(r"(аниме|манга|naruto|one piece|tokyo ghoul)", all_text):
                topic_keywords["аниме"] = True
            if re.search(r"(игр|играть|game|steam|плейстейшн)", all_text):
                topic_keywords["игры"] = True
            if re.search(r"(путешеств|путешеств|поездк|отдых|море|путеш)", all_text):
                topic_keywords["путешествия"] = True
            if re.search(r"(спорт|зал|бег|фитнес|тренеровк)", all_text):
                topic_keywords["спорт"] = True
            if re.search(r"(друг|подруг|товарищ|компания)", all_text):
                topic_keywords["друзья"] = True
            if re.search(r"(кот|собак|питом|животн|кошк|хомяк)", all_text):
                topic_keywords["питомцы"] = True

            for key, found in topic_keywords.items():
                if found:
                    topics.append(key)

            snippet = old_messages[:30]
            last_msg_label = "Пользователь" if old_messages[-1].startswith("Пользователь") else "Мия"
            last_text = old_messages[-1].split(": ", 1)[-1][:100] if old_messages else ""

            summary = (
                f"Ранее в переписке ({dropped_count} сообщений) "
                f"обсуждали: {', '.join(topics) if topics else 'общие темы'}. "
                f"Примеры фраз из начала диалога: {snippet[:3]}"
            )
            summary_parts.append(summary)

    messages: List[Dict[str, str]] = []

    if summary_parts:
        summary_text = "\n".join(summary_parts)
        messages.append({
            "role": "system",
            "content": SYSTEM_PROMPT + "\n\n=== КРАТКАЯ СВОДКА ПРОШЛЫХ ДИАЛОГОВ ===\n" + summary_text + "\n=== КОНЕЦ СВОДКИ ===",
        })
    else:
        messages.append(system_msg)

    for idx in kept_indices:
        row = history[idx]
        role = row.get("role", "user")
        content = row.get("content", "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    return messages


def build_groq_messages(history: List[Dict[str, Any]], new_user_message: str) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    for row in history:
        role = row.get("role", "user")
        content = row.get("content", "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": new_user_message})
    return messages


def fit_messages_to_context(messages: List[Dict[str, str]], max_tokens: int) -> List[Dict[str, str]]:
    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    user_msg = messages[-1] if messages else None

    if not system_msg or not user_msg or len(messages) <= 2:
        return messages

    system_tokens = estimate_tokens(system_msg.get("content", ""))
    user_tokens = estimate_tokens(user_msg.get("content", ""))
    fixed_tokens = system_tokens + user_tokens + 20

    history_msgs = messages[1:-1]
    total_history_tokens = 0
    kept: List[Dict[str, str]] = []

    for msg in reversed(history_msgs):
        msg_tokens = estimate_tokens(msg.get("content", "")) + 4
        if total_history_tokens + msg_tokens > max_tokens - fixed_tokens:
            break
        total_history_tokens += msg_tokens
        kept.append(msg)

    kept.reverse()

    result: List[Dict[str, str]] = [system_msg] + kept + [user_msg]
    return result


def call_groq(messages: List[Dict[str, str]]) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("Groq API key not configured")

    client = Groq(api_key=GROQ_API_KEY)

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=1.0,
        top_p=0.95,
        frequency_penalty=0.55,
        presence_penalty=0.35,
        max_tokens=500,
    )

    choice = response.choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return str(content or "").strip()


def split_response(text: str, max_parts: int = 3) -> List[str]:
    if not text:
        return [random.choice(["ну привет", "ага", "ну хай"])]

    text = re.sub(r"\n{2,}", "\n", text.strip())
    raw_parts = [p.strip() for p in re.split(r"\n|(?<=[.!?])\s+", text) if p.strip()]

    if len(raw_parts) <= max_parts:
        return raw_parts

    merged: List[str] = []
    bucket = ""

    for part in raw_parts:
        if not bucket:
            bucket = part
            continue
        if len(bucket) + len(part) + 1 <= 160:
            bucket = f"{bucket} {part}"
        else:
            merged.append(bucket)
            bucket = part

    if bucket:
        merged.append(bucket)

    if len(merged) <= max_parts:
        return merged

    total = " ".join(merged)
    approx = max(1, len(total) // max_parts)
    hard_split: List[str] = []
    cursor = 0

    for _ in range(max_parts - 1):
        end = min(len(total), cursor + approx)
        while end < len(total) and total[end] != " ":
            end += 1
        hard_split.append(total[cursor:end].strip())
        cursor = end

    hard_split.append(total[cursor:].strip())
    return [p for p in hard_split if p]


def build_failure_message() -> str:
    return random.choice([
        "ой, что-то у меня тупняк, повтори",
        "щас, у меня мозг завис",
        "блин, не сейчас, что-то с интернетом",
    ])


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True})


@app.get("/api/history")
def api_history() -> Any:
    user_id = str(request.args.get("user_id", "default-user")).strip() or "default-user"
    db_conn = get_db_connection()

    if db_conn is None:
        return jsonify({"messages": []})

    try:
        with db_conn:
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text, role, content, created_at
                    FROM chat_messages
                    WHERE user_id = %s
                    ORDER BY created_at ASC
                    LIMIT 500
                    """,
                    (user_id,),
                )
                rows = list(cur.fetchall() or [])

        return jsonify({"messages": rows})
    finally:
        db_conn.close()


@app.get("/api/export")
def api_export() -> Any:
    user_id = str(request.args.get("user_id", "default-user")).strip() or "default-user"
    db_conn = get_db_connection()

    if db_conn is None:
        return jsonify({"error": "database unavailable"}), 500

    try:
        messages = fetch_all_messages_full(db_conn, user_id)

        if not messages:
            return jsonify({"text": "Пока нет сообщений.", "count": 0})

        lines: List[str] = []
        lines.append("=== Экспорт чата с Мией ===")
        lines.append(f"Пользователь: {user_id}")
        lines.append(f"Всего сообщений: {len(messages)}")
        lines.append("")

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "").strip()
            created = msg.get("created_at")
            time_str = ""
            if isinstance(created, datetime):
                time_str = created.strftime("%d.%m.%Y %H:%M")
            elif created:
                time_str = str(created)[:16]

            prefix = "Ты" if role == "user" else "Мия"
            if time_str:
                lines.append(f"[{time_str}] {prefix}: {content}")
            else:
                lines.append(f"{prefix}: {content}")

        lines.append("")
        lines.append("=== Конец переписки ===")

        return jsonify({
            "text": "\n".join(lines),
            "count": len(messages),
        })
    finally:
        db_conn.close()


@app.post("/api/chat")
def api_chat() -> Any:
    payload = request.get_json(silent=True) or {}
    user_text = str(payload.get("message", "")).strip()
    user_id = str(payload.get("user_id", "default-user")).strip() or "default-user"

    if not user_text:
        return jsonify({"messages": ["ну напиши что-нибудь"]}), 400

    if not GROQ_API_KEY:
        return jsonify({"messages": [build_failure_message()]}), 500

    db_conn = get_db_connection()
    history: List[Dict[str, Any]] = []

    if db_conn is not None:
        try:
            with db_conn:
                history = fetch_all_messages(db_conn, user_id, limit=2000)
        except Exception:
            history = []

    total_history_chars = sum(len(m.get("content", "")) for m in history)
    total_history_tokens = total_history_chars // CHARS_PER_TOKEN

    if total_history_tokens > MAX_CONTEXT_TOKENS:
        groq_messages = compress_history(history, MAX_CONTEXT_TOKENS)
        groq_messages.append({"role": "user", "content": user_text})
    else:
        groq_messages = build_groq_messages(history, user_text)
        groq_messages = fit_messages_to_context(groq_messages, MAX_CONTEXT_TOKENS)

    try:
        ai_text = call_groq(groq_messages)
    except Exception:
        if db_conn is not None:
            try:
                with db_conn:
                    save_message(db_conn, user_id, "user", user_text)
                    save_message(db_conn, user_id, "assistant", build_failure_message())
            except Exception:
                pass
            finally:
                db_conn.close()

        return jsonify({"messages": [build_failure_message()]}), 502

    if not ai_text:
        ai_text = random.choice(["ну и что", "хз что сказать", "ага"])

    messages = split_response(ai_text)

    if db_conn is not None:
        try:
            with db_conn:
                save_message(db_conn, user_id, "user", user_text)
                for msg in messages:
                    save_message(db_conn, user_id, "assistant", msg)
        except Exception:
            pass
        finally:
            db_conn.close()

    return jsonify({"messages": messages})


init_database()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
