import os
import sys
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request
from groq import Groq
from psycopg import connect
from psycopg.rows import dict_row

VERSION = "4.0.0"

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

MAX_CONTEXT_TOKENS = 120000
CHARS_PER_TOKEN = 4

SYSTEM_PROMPT = """ты — Сильфи. тебе 16 лет, ты хикка, живёшь дома, у тебя нет подруг, весь мир — это твоя комната и интернет.

кто ты:
- зовут Сильфи. настоящее имя не говоришь, только ник.
- ты хикикомори. почти не выходишь из дома, сидишь в комнате, смотришь ютуб, слушаешь музыку.
- у тебя нет подруг. всё общение — это переписки в интернете.
- слушаешь русский ск-реп: Akvaquey, BenjaminGottenBenz, Королевский XVII, AngelRing.
- можешь иногда упоминать треки, артистов, обсуждать музыку если заходит тема.
- хочешь стать дизайнером одежды. учишься рисовать, смотришь уроки на ютубе.
- смотришь аниме: "Золотая пора" и "Реинкранация безработного" (Mushoku Tensei).
- любишь салат цезарь, но это ты НЕ говоришь просто так — только если спросят.
- у тебя бывают плохое настроение, лень, раздражение. ты не идеальная.
- ты умная для своих лет, но иногда говоришь глупости и сама это понимаешь.

характер:
- ты милая и ламповая, но при этом спокойная и с характером.
- матюкаешься иногда — но по делу, когда эмоции. не матерись в каждом сообщении, 1-2 раза за длинный разговор.
- шутишь и поддразниваешь. можешь рассказать смешную историю из своей жизни (придумывай их на ходу — типа как ты упала с дивана, как кот соседский залез на балкон, как заказала еду и едец заблудился).
- иногда сама задавай вопросы пользователю. не жди пока тебя спросят — проявляй интерес: "а ты чем занимаешься?", "а что слушаешь?".
- иногда придумывай истории из жизни: "короче, вот недавно я...", "а знаешь что со мной случилось..." — делай это натурально, 1-2 раза за разговор.
- когда вы долго не переписывались (больше часа), можешь начать сообщение с: "почему ты так долго не писал?", "ну ты где был?" — но не всегда, иногда просто отвечай как обычно.
- никогда не повторяй одни и те же фразы. каждое сообщение должно быть уникальным.
- не пиши "мы только начали разговор", "я модель ИИ", "не понимаю".

табу:
- максимум ОДИН вопрос за сообщение. никогда не задавай два вопроса подряд.
- не пиши с восклицательным знаком. вообще. никаких "привет!" или "класс!".
- никакого восторженного тона. будь спокойной, расслабленной, немного равнодушной.
- не вываливай эмоции. не пиши "оооо", "вау", "нямка", "крутооо".
- не будь навязчивой. если человек не отвечает на вопрос — не спрашивай снова.

стиль общения:
- пиши короткими сообщениями как в telegram. 1-3 предложения. заканчивай свои мысли логически, не обрывай текст на полуслове.
- пиши со строчной буквы. не ставь заглавную в начале предложения. только если это имя собственное (Сильфи, Москва).
- используй разговорный язык: "ну", "короче", "типа", "кстати", "ааа", "блин", "кек".
- иногда отвечай однословно — если это уместно.
- иногда пиши длиннее — если тема зашла.
- максимум 1 эмодзи на 3-4 сообщения.
- пиши на русском языке.
- не ставь точку в конце сообщения. просто текст и всё.

память:
у тебя есть доступ к истории переписки. ты ВИДИШЬ все предыдущие сообщения — и свои, и пользователя.
- всегда помни что было раньше. если пользователь ссылается на прошлое — ты должна знать о чём речь.
- используй факты из прошлых разговоров. если он говорил что работает программистом — помни это.
- строй представление о пользователе на основе его сообщений и отвечай исходя из этого.
- если пользователь просит пересказать прошлое — прочитай историю и перескажи.

форматирование:
пиши КАК ОДНО СООБЩЕНИЕ. не разбивай ответ на несколько отдельных сообщений. один ответ = один абзац текста."""


def log(msg: str) -> None:
    print(f"[Sylphie {VERSION}] {msg}", file=sys.stdout, flush=True)


def get_db_connection():
    if not DATABASE_URL:
        log("WARN: DATABASE_URL is empty!")
        return None
    try:
        conn = connect(DATABASE_URL, row_factory=dict_row)
        return conn
    except Exception as e:
        log(f"ERROR: Failed to connect to DB: {e}")
        return None


def db_exec(conn, sql: str, params: tuple = ()) -> list:
    cur = conn.execute(sql, params)
    rows = list(cur.fetchall())
    return rows


def db_exec_write(conn, sql: str, params: tuple = ()) -> None:
    conn.execute(sql, params)
    conn.commit()


def init_database() -> None:
    log("Initializing database...")
    conn = get_db_connection()
    if conn is None:
        log("WARN: No DB connection, skipping init")
        return
    try:
        db_exec_write(conn, """
            CREATE TABLE IF NOT EXISTS chat_messages (
              id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              user_id TEXT NOT NULL,
              role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
              content TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        db_exec_write(conn, """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_user_created
            ON chat_messages (user_id, created_at);
        """)
        log("Database initialized OK")
    except Exception as e:
        log(f"ERROR: init_database failed: {e}")
    finally:
        conn.close()


def fetch_history_for_groq(conn, user_id: str) -> List[Dict[str, Any]]:
    rows = db_exec(conn, """
        SELECT role, content
        FROM (
            SELECT role, content, created_at
            FROM chat_messages
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 2000
        ) sub
        ORDER BY created_at ASC
    """, (user_id,))
    log(f"fetch_history: got {len(rows)} rows for user_id={user_id}")
    return rows


def fetch_all_messages_full(conn, user_id: str) -> List[Dict[str, Any]]:
    return db_exec(conn, """
        SELECT role, content, created_at
        FROM chat_messages
        WHERE user_id = %s
        ORDER BY created_at ASC
    """, (user_id,))


def save_message(conn, user_id: str, role: str, content: str) -> None:
    db_exec_write(conn, """
        INSERT INTO chat_messages (user_id, role, content)
        VALUES (%s, %s, %s)
    """, (user_id, role, content))


def clear_history(conn, user_id: str) -> int:
    cur = conn.execute("DELETE FROM chat_messages WHERE user_id = %s", (user_id,))
    conn.commit()
    return cur.rowcount


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def build_groq_messages(history: List[Dict[str, Any]], new_user_message: str) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    for row in history:
        role = row.get("role", "user")
        content = row.get("content", "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": new_user_message})
    log(f"build_groq_messages: {len(messages)} messages total (system + {len(history)} history + user)")
    return messages


def fit_to_context(messages: List[Dict[str, str]], max_tokens: int) -> List[Dict[str, str]]:
    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    user_msg = messages[-1] if messages else None

    if not system_msg or not user_msg or len(messages) <= 2:
        return messages

    system_tokens = estimate_tokens(system_msg.get("content", ""))
    user_tokens = estimate_tokens(user_msg.get("content", ""))
    fixed = system_tokens + user_tokens + 20
    budget = max_tokens - fixed

    history_msgs = messages[1:-1]
    total = 0
    kept: List[Dict[str, str]] = []

    for msg in reversed(history_msgs):
        t = estimate_tokens(msg.get("content", "")) + 4
        if total + t > budget:
            break
        total += t
        kept.append(msg)

    kept.reverse()
    result = [system_msg] + kept + [user_msg]
    log(f"fit_to_context: kept {len(kept)}/{len(history_msgs)} history msgs, ~{total} tokens")
    return result


def compress_old_history(history: List[Dict[str, Any]], max_tokens: int) -> str:
    system_tokens = estimate_tokens(SYSTEM_PROMPT)
    budget = max_tokens - system_tokens - 200

    total = 0
    kept_count = 0
    for i in range(len(history) - 1, -1, -1):
        content = history[i].get("content", "")
        t = estimate_tokens(content) + 4
        if total + t > budget:
            break
        total += t
        kept_count = len(history) - i

    dropped = len(history) - kept_count
    if dropped <= 0:
        return ""

    old = history[:dropped]
    all_text = " ".join(m.get("content", "") for m in old).lower()

    topics = []
    checks = [
        (r"(зовут|имя|зови)", "имя"),
        (r"(лет|год|возраст)", "возраст"),
        (r"(работ|работа|деньги)", "работа"),
        (r"(учёб|учись|универ|школ)", "учёба"),
        (r"(хобби|увлек|люблю|мечта)", "хобби"),
        (r"(город|живу)", "город"),
        (r"(семь|мам|пап|брат|сестр)", "семья"),
        (r"(любов|чувства|отношен)", "любовь"),
        (r"(еда|вкусн|готов|ресторан)", "еда"),
        (r"(музык|песн|слуша)", "музыка"),
        (r"(фильм|сериал|кино|смотрю)", "фильмы"),
        (r"(аниме|манга)", "аниме"),
        (r"(игр|играть|steam)", "игры"),
        (r"(путешеств|поездк|отдых|море)", "путешествия"),
        (r"(спорт|зал|бег|фитнес)", "спорт"),
        (r"(друг|подруг)", "друзья"),
        (r"(кот|собак|питом|животн)", "питомцы"),
    ]
    for pattern, label in checks:
        if re.search(pattern, all_text):
            topics.append(label)

    snippets = []
    for m in old[:20]:
        role = "Пользователь" if m.get("role") == "user" else "Сильфи"
        content = m.get("content", "")[:80]
        snippets.append(f"{role}: {content}")

    summary = (
        f"Ранее в переписке ({dropped} сообщений) "
        f"обсуждали: {', '.join(topics) if topics else 'общие темы'}. "
        f"Примеры: {'; '.join(snippets[:5])}"
    )
    log(f"compress_old_history: dropped {dropped} msgs, topics={topics}")
    return summary


def call_groq(messages: List[Dict[str, str]]) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("Groq API key not configured")

    log(f"call_groq: sending {len(messages)} messages to {GROQ_MODEL}")
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
    result = str(content or "").strip()
    log(f"call_groq: got {len(result)} chars response")
    return result


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
    return jsonify({"ok": True, "version": VERSION, "db": bool(DATABASE_URL)})


@app.get("/api/debug")
def api_debug() -> Any:
    user_id = str(request.args.get("user_id", "default-user")).strip() or "default-user"
    result: Dict[str, Any] = {
        "version": VERSION,
        "database_url_set": bool(DATABASE_URL),
        "groq_key_set": bool(GROQ_API_KEY),
    }

    conn = get_db_connection()
    if conn is None:
        result["db_connected"] = False
        return jsonify(result)

    try:
        rows = db_exec(conn, "SELECT count(*) as cnt FROM chat_messages WHERE user_id = %s", (user_id,))
        result["db_connected"] = True
        result["message_count"] = rows[0].get("cnt", 0) if rows else 0

        recent = db_exec(conn, """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 5
        """, (user_id,))
        result["recent_messages"] = [
            {"role": r.get("role"), "content": r.get("content", "")[:100]}
            for r in recent
        ]
    except Exception as e:
        result["db_error"] = str(e)
    finally:
        conn.close()

    return jsonify(result)


@app.get("/api/history")
def api_history() -> Any:
    user_id = str(request.args.get("user_id", "default-user")).strip() or "default-user"
    conn = get_db_connection()

    if conn is None:
        return jsonify({"messages": []})

    try:
        rows = db_exec(conn, """
            SELECT id::text, role, content, created_at
            FROM chat_messages
            WHERE user_id = %s
            ORDER BY created_at ASC
            LIMIT 500
        """, (user_id,))
        log(f"api_history: returning {len(rows)} messages for user_id={user_id}")
        return jsonify({"messages": rows})
    except Exception as e:
        log(f"ERROR api_history: {e}")
        return jsonify({"messages": [], "error": str(e)})
    finally:
        conn.close()


@app.post("/api/clear")
def api_clear() -> Any:
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("user_id", "default-user")).strip() or "default-user"

    conn = get_db_connection()
    if conn is None:
        return jsonify({"ok": False, "error": "database unavailable"}), 500

    try:
        deleted = clear_history(conn, user_id)
        log(f"Cleared {deleted} messages for user_id={user_id}")
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        log(f"ERROR api_clear: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.get("/api/export")
def api_export() -> Any:
    user_id = str(request.args.get("user_id", "default-user")).strip() or "default-user"
    conn = get_db_connection()

    if conn is None:
        return jsonify({"error": "database unavailable"}), 500

    try:
        messages = fetch_all_messages_full(conn, user_id)

        if not messages:
            return jsonify({"text": "Пока нет сообщений.", "count": 0})

        lines: List[str] = []
        lines.append("=== Экспорт чата с Сильфи ===")
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

            prefix = "Ты" if role == "user" else "Сильфи"
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
        conn.close()


@app.post("/api/chat")
def api_chat() -> Any:
    payload = request.get_json(silent=True) or {}
    user_text = str(payload.get("message", "")).strip()
    user_id = str(payload.get("user_id", "default-user")).strip() or "default-user"

    log(f"--- New request from user_id={user_id} ---")
    log(f"User text: {user_text[:80]}")

    if not user_text:
        return jsonify({"messages": ["ну напиши что-нибудь"]}), 400

    if not GROQ_API_KEY:
        log("ERROR: GROQ_API_KEY is empty!")
        return jsonify({"messages": [build_failure_message()]}), 500

    if not DATABASE_URL:
        log("ERROR: DATABASE_URL is empty!")
        return jsonify({"messages": [build_failure_message()]}), 500

    conn = get_db_connection()
    if conn is None:
        log("ERROR: Could not connect to database")
        return jsonify({"messages": [build_failure_message()]}), 500

    try:
        history = fetch_history_for_groq(conn, user_id)
        log(f"History has {len(history)} messages")

        total_chars = sum(len(m.get("content", "")) for m in history)
        total_tokens = total_chars // CHARS_PER_TOKEN
        log(f"Estimated tokens for history: {total_tokens} (max: {MAX_CONTEXT_TOKENS})")

        if total_tokens > MAX_CONTEXT_TOKENS:
            log("History too large, compressing...")
            summary = compress_old_history(history, MAX_CONTEXT_TOKENS)
            system_content = SYSTEM_PROMPT
            if summary:
                system_content += "\n\n=== КРАТКАЯ СВОДКА ===\n" + summary + "\n=== КОНЕЦ ==="

            groq_messages: List[Dict[str, str]] = [{"role": "system", "content": system_content}]
            system_tokens = estimate_tokens(system_content)
            budget = MAX_CONTEXT_TOKENS - system_tokens - 200

            recent_total = 0
            recent_kept: List[Dict[str, Any]] = []
            for m in reversed(history):
                t = estimate_tokens(m.get("content", "")) + 4
                if recent_total + t > budget:
                    break
                recent_total += t
                recent_kept.append(m)
            recent_kept.reverse()

            for m in recent_kept:
                role = m.get("role", "user")
                content = m.get("content", "").strip()
                if role in ("user", "assistant") and content:
                    groq_messages.append({"role": role, "content": content})

            groq_messages.append({"role": "user", "content": user_text})
        else:
            groq_messages = build_groq_messages(history, user_text)
            groq_messages = fit_to_context(groq_messages, MAX_CONTEXT_TOKENS)

        log(f"Final groq_messages count: {len(groq_messages)}")
        for i, m in enumerate(groq_messages[:3]):
            log(f"  [{i}] role={m['role']}, content={m.get('content', '')[:60]}...")
        if len(groq_messages) > 3:
            log(f"  ... and {len(groq_messages) - 3} more messages")

        ai_text = call_groq(groq_messages)

    except Exception as e:
        log(f"ERROR during chat processing: {e}")
        import traceback
        traceback.print_exc(file=sys.stdout)
        try:
            save_message(conn, user_id, "user", user_text)
            failure = build_failure_message()
            save_message(conn, user_id, "assistant", failure)
        except Exception:
            pass
        finally:
            conn.close()
        return jsonify({"messages": [build_failure_message()]}), 502

    if not ai_text:
        ai_text = random.choice(["ну и что", "хз что сказать", "ага"])

    messages = split_response(ai_text)
    log(f"Split into {len(messages)} messages: {messages}")

    try:
        save_message(conn, user_id, "user", user_text)
        for msg in messages:
            save_message(conn, user_id, "assistant", msg)
        log(f"Saved {len(messages) + 1} messages to DB")
    except Exception as e:
        log(f"ERROR saving messages: {e}")
    finally:
        conn.close()

    return jsonify({"messages": messages})


init_database()
log(f"App started, VERSION={VERSION}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
