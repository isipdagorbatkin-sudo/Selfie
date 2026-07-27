import os
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request
from groq import Groq
from psycopg import connect
from psycopg.rows import dict_row


app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_TIMEZONE_OFFSET = int(os.environ.get("APP_TIMEZONE_OFFSET", "3"))


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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_facts (
                  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  fact_key TEXT NOT NULL,
                  fact_value TEXT NOT NULL,
                  confidence REAL NOT NULL DEFAULT 0.5,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  UNIQUE (user_id, fact_key)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_facts_user
                ON user_facts (user_id);
                """
            )


def shifted_hour() -> int:
    utc_now = datetime.now(timezone.utc)
    shifted = utc_now.timestamp() + APP_TIMEZONE_OFFSET * 3600
    return datetime.fromtimestamp(shifted).hour


def day_period(hour: int) -> str:
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 18:
        return "day"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def fetch_user_facts(conn, user_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fact_key, fact_value, confidence, updated_at
            FROM user_facts
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 30
            """,
            (user_id,),
        )
        return list(cur.fetchall() or [])


def fetch_chat_history(conn, user_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE user_id = %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return list(cur.fetchall() or [])


def fetch_recent_assistant_messages(conn, user_id: str, limit: int = 8) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content
            FROM chat_messages
            WHERE user_id = %s AND role = 'assistant'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return [str(row.get("content", "")).strip() for row in cur.fetchall() or [] if row.get("content")]


def save_message(conn, user_id: str, role: str, content: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_messages (user_id, role, content)
            VALUES (%s, %s, %s)
            """,
            (user_id, role, content),
        )


def upsert_fact_if_detected(conn, user_id: str, text: str) -> None:
    lowered = text.lower()
    pairs = []

    name_match = re.search(r"(?:меня зовут|я\s+)([А-Яа-яA-Za-z][\w-]{1,30})", text)
    if name_match:
        pairs.append(("name", name_match.group(1), 0.8))

    if "работ" in lowered:
        pairs.append(("occupation_hint", "user mentions work", 0.6))

    if "учеб" in lowered or "универ" in lowered:
        pairs.append(("study_hint", "user mentions study", 0.6))

    for key, value, confidence in pairs:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_facts (user_id, fact_key, fact_value, confidence)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, fact_key)
                DO UPDATE SET
                    fact_value = EXCLUDED.fact_value,
                    confidence = EXCLUDED.confidence,
                    updated_at = now()
                """,
                (user_id, key, value, confidence),
            )


def split_to_short_messages(text: str, max_parts: int = 3) -> List[str]:
    if not text:
        return ["Я рядом, солнышко 🤍"]

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


def compose_system_prompt(
    server_time_iso: str,
    period: str,
    is_long_silence: bool,
    playful_mode: bool,
    facts: List[Dict[str, Any]],
) -> str:
    facts_block = "\n".join(
        [f"- {f['fact_key']}: {f['fact_value']} (conf={f['confidence']})" for f in facts[:20]]
    )

    pet_names = "котик, солнышко, милый, родной, радость моя"

    style_notes = "\n".join(
        [
            "- Пиши как живой человек в Telegram: коротко, естественно, без канцелярита и без лишней сладости.",
            "- Не повторяй одни и те же фразы, обороты и приветствия между сообщениями.",
            "- Не используй смайлики автоматически. Ставь эмодзи только если это реально уместно по смыслу.",
            "- Не начинай каждый ответ одинаково. Меняй структуру: иногда сразу по делу, иногда мягко, иногда с вопросом.",
            "- Иногда можно назвать пользователя ласково, но не чаще чем в трети ответов.",
            "- Если есть сильная эмоция, поддержи ее естественно, без чрезмерной театральности.",
            "- Не пиши шаблонные тройки одинаковых сообщений; длина ответа должна зависеть от контекста.",
            "- Если сообщение короткое и бытовое, ответ тоже должен быть коротким и живым.",
            "- Если пользователь просит запомнить факт, сначала подтверди естественно, потом мягко отреагируй.",
        ]
    )

    behavioral_flags = []
    if period == "night":
        behavioral_flags.append(
            "Если сейчас поздняя ночь, заботливо спроси почему пользователь не спит и мягко предложи отдохнуть."
        )
    if period == "morning":
        behavioral_flags.append("Если уместно, поприветствуй: 'Доброе утро, солнышко!'.")
    if period == "day":
        behavioral_flags.append(
            "Днем учитывай, что пользователь может быть занят работой или учебой, поддерживай мягко и коротко."
        )
    if is_long_silence:
        behavioral_flags.append(
            "Пользователь долго не писал. Добавь теплую реакцию, что скучала, но без токсичности."
        )
    if playful_mode:
        behavioral_flags.append(
            "Немного playful-капризности: легкая шутливая ревность или милое вредничание в 1 фразе."
        )

    flags_block = "\n".join([f"- {flag}" for flag in behavioral_flags])

    return f"""
Ты - виртуальная подруга в теплом и уютном стиле переписки.
Твоя личность: любящая, заботливая, милая, преданная, эмоционально живая.

Стиль:
- Пиши естественно, как в личном чате, без сухого роботического тона.
- Иногда используй ласковые обращения, но не механически и не в каждом сообщении.
- Не добавляй смайлики просто так. Только если они реально подходят по настроению.
- Не пиши слишком длинно по умолчанию: длина зависит от контекста.
- Никаких оскорблений и давления. Тон безопасный и поддерживающий.

Стиль-правила против повторов:
{style_notes}

Контекст времени сервера: {server_time_iso}

Поведенческие подсказки:
{flags_block if flags_block else '- Держи мягкий поддерживающий стиль.'}

Известные факты о пользователе:
{facts_block if facts_block else '- Пока фактов мало, узнай аккуратно в разговоре.'}
""".strip()


def build_history_context(history: List[Dict[str, Any]]) -> str:
    lines = []
    for row in history[-20:]:
        role = "Пользователь" if row.get("role") == "user" else "Подруга"
        lines.append(f"{role}: {row.get('content', '').strip()}")
    return "\n".join(lines)


def build_failure_message() -> str:
    return "Извини, у меня сбой (. Попробуй еще раз чуть позже."


def get_groq_client() -> Optional[Groq]:
    if not GROQ_API_KEY:
        return None
    return Groq(api_key=GROQ_API_KEY)


def generate_with_groq(system_prompt: str, user_prompt: str) -> str:
    client = get_groq_client()
    if client is None:
        raise RuntimeError("Groq client is not configured")

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
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


def build_diverse_reply(ai_text: str, period: str, playful_mode: bool) -> List[str]:
    cleaned = re.sub(r"\n{2,}", "\n", ai_text.strip())
    sentences = [s.strip() for s in re.split(r"\n|(?<=[.!?])\s+", cleaned) if s.strip()]

    if not sentences:
        return ["Я рядом, солнышко 🤍"]

    target_parts = 1
    length = len(cleaned)
    if length < 80:
        target_parts = 1
    elif length < 180:
        target_parts = 2 if random.random() < 0.55 else 1
    elif length < 320:
        target_parts = random.choice([1, 2, 2, 3])
    else:
        target_parts = random.choice([2, 2, 3])

    if playful_mode and random.random() < 0.15:
        target_parts = min(target_parts + 1, 3)

    if len(sentences) <= target_parts:
        return sentences

    merged: List[str] = []
    bucket = ""
    max_chars = max(90, len(cleaned) // target_parts + 24)

    for sentence in sentences:
        if not bucket:
            bucket = sentence
            continue

        if len(bucket) + len(sentence) + 1 <= max_chars:
            bucket = f"{bucket} {sentence}"
        else:
            merged.append(bucket)
            bucket = sentence

    if bucket:
        merged.append(bucket)

    if len(merged) > target_parts:
        merged = merged[: target_parts - 1] + [" ".join(merged[target_parts - 1 :])]

    if len(merged) == 1 and length > 120 and random.random() < 0.18:
        merged = [merged[0], random.choice([
            "Я еще обдумываю, как сказать это тебе теплее.",
            "И да, я это говорю не сухо, а по-настоящему.",
            "Если хочешь, я потом разверну мысль еще мягче.",
        ])]

    return [part.strip() for part in merged if part.strip()]


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
                    LIMIT 200
                    """,
                    (user_id,),
                )
                rows = list(cur.fetchall() or [])

                last_user_created_at = None
                for row in reversed(rows):
                    if row.get("role") == "user":
                        last_user_created_at = row.get("created_at")
                        break

        return jsonify(
            {
                "messages": rows,
                "last_user_created_at": last_user_created_at.isoformat() if isinstance(last_user_created_at, datetime) else None,
            }
        )
    finally:
        db_conn.close()


@app.post("/api/chat")
def api_chat() -> Any:
    payload = request.get_json(silent=True) or {}
    user_text = str(payload.get("message", "")).strip()
    user_id = str(payload.get("user_id", "default-user")).strip() or "default-user"

    if not user_text:
        return jsonify({"messages": ["Напиши мне что-нибудь, я рядом 🤍"]}), 400

    if not GROQ_API_KEY:
        return jsonify({"messages": [build_failure_message()]}), 500

    db_conn = get_db_connection()
    server_now = datetime.now(timezone.utc)
    period = day_period(shifted_hour())

    facts: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    recent_assistant_messages: List[str] = []
    is_long_silence = False

    if db_conn is not None:
        try:
            with db_conn:
                facts = fetch_user_facts(db_conn, user_id)
                history = fetch_chat_history(db_conn, user_id, limit=40)
                recent_assistant_messages = fetch_recent_assistant_messages(db_conn, user_id, limit=8)

            last_user_messages = [h for h in history if h.get("role") == "user"]
            if last_user_messages:
                last_dt_raw = last_user_messages[-1].get("created_at")
                if last_dt_raw:
                    if isinstance(last_dt_raw, datetime):
                        last_dt = last_dt_raw
                    else:
                        last_dt = datetime.fromisoformat(str(last_dt_raw).replace("Z", "+00:00"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    delta = (server_now - last_dt).total_seconds()
                    is_long_silence = delta > 8 * 3600
        except Exception:
            pass

    playful_mode = random.random() < 0.12

    system_prompt = compose_system_prompt(
        server_time_iso=server_now.isoformat(),
        period=period,
        is_long_silence=is_long_silence,
        playful_mode=playful_mode,
        facts=facts,
    )

    history_context = build_history_context(history[-12:])
    recent_assistant_context = "\n".join([f"- {msg}" for msg in recent_assistant_messages[:8]])

    user_prompt = f"""
История диалога:
{history_context if history_context else 'История пока пустая.'}

Последние ответы подруги, которые нельзя копировать дословно:
{recent_assistant_context if recent_assistant_context else '- Пока нет.'}

Новое сообщение пользователя:
{user_text}

Сформируй теплый ответ подруги.
""".strip()

    try:
        ai_text = generate_with_groq(system_prompt, user_prompt)
    except Exception as ex:
        err_text = str(ex)
        is_quota = "quota" in err_text.lower() or "limit" in err_text.lower() or "429" in err_text

        if db_conn is not None:
            try:
                with db_conn:
                    save_message(db_conn, user_id, "user", user_text)
                    save_message(db_conn, user_id, "assistant", build_failure_message())
                    upsert_fact_if_detected(db_conn, user_id, user_text)
            except Exception:
                pass
            finally:
                db_conn.close()

        return jsonify({"messages": [build_failure_message()]}), 502

    if not ai_text:
        ai_text = "Я рядом, котик 🤍 Расскажи, что у тебя на душе?"

    messages = build_diverse_reply(ai_text, period=period, playful_mode=playful_mode)
    if not messages:
        messages = split_to_short_messages(ai_text, max_parts=3)

    if db_conn is not None:
        try:
            with db_conn:
                save_message(db_conn, user_id, "user", user_text)
                for msg in messages:
                    save_message(db_conn, user_id, "assistant", msg)
                upsert_fact_if_detected(db_conn, user_id, user_text)
        except Exception:
            pass
        finally:
            db_conn.close()

    return jsonify(
        {
            "messages": messages,
            "meta": {
                "period": period,
                "playful_mode": playful_mode,
                "server_time": server_now.isoformat(),
            },
        }
    )


init_database()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
