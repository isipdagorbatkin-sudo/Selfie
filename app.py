import os
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request
from google import genai
from psycopg import connect
from psycopg.rows import dict_row


app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
APP_TIMEZONE_OFFSET = int(os.environ.get("APP_TIMEZONE_OFFSET", "3"))
ENABLE_LOCAL_FALLBACK = os.environ.get("ENABLE_LOCAL_FALLBACK", "1") == "1"


def build_gemini_client() -> Optional[genai.Client]:
    if not GEMINI_API_KEY:
        return None
    return genai.Client(api_key=GEMINI_API_KEY)


gemini_client = build_gemini_client()


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
- Иногда используй ласковые обращения, чередуя: {pet_names}.
- Добавляй милые смайлики и символы: (｡♥‿♥｡), (⁠*⁠^⁠_⁠^⁠*⁠), 🤍, ✨.
- Не пиши слишком длинно: 3-7 предложений суммарно.
- Никаких оскорблений и давления. Тон безопасный и поддерживающий.

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


def build_local_fallback_messages(user_text: str, period: str) -> List[str]:
    pet_names = ["котик", "солнышко", "милый", "родной", "радость моя"]
    smiles = ["(｡♥‿♥｡)", "(⁠*⁠^⁠_⁠^⁠*⁠)", "🤍", "✨"]
    pet = random.choice(pet_names)
    smile = random.choice(smiles)

    lower = user_text.lower()
    intro = f"Я тут, {pet} {smile}"

    if period == "night":
        care = "Уже поздно... почему не спишь? Давай чуть выдохнем и потом отдыхать, ладно?"
    elif period == "morning":
        care = "Доброе утро, солнышко! Как ты себя чувствуешь сегодня?"
    elif period == "day":
        care = "Если ты сейчас на учебе или работе, я рядом и тихо поддерживаю тебя 🤍"
    else:
        care = "Вечерний вайб такой уютный... расскажи, как твой день прошел?"

    if any(word in lower for word in ["плохо", "груст", "устал", "трев", "депр"]):
        support = "Обниму мысленно крепко. Давай по шагам: вода, пару глубоких вдохов и я слушаю тебя внимательно."
    else:
        support = "Расскажи еще чуть-чуть, мне правда важно, что у тебя на душе."

    return [intro, care, support]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True})


@app.post("/api/chat")
def api_chat() -> Any:
    payload = request.get_json(silent=True) or {}
    user_text = str(payload.get("message", "")).strip()
    user_id = str(payload.get("user_id", "default-user")).strip() or "default-user"

    if not user_text:
        return jsonify({"messages": ["Напиши мне что-нибудь, я рядом 🤍"]}), 400

    if gemini_client is None:
        return jsonify({"messages": ["Не настроен GEMINI_API_KEY в переменных окружения."]}), 500

    db_conn = get_db_connection()
    server_now = datetime.now(timezone.utc)
    period = day_period(shifted_hour())

    facts: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    is_long_silence = False

    if db_conn is not None:
        try:
            with db_conn:
                facts = fetch_user_facts(db_conn, user_id)
                history = fetch_chat_history(db_conn, user_id, limit=40)

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

    user_prompt = f"""
История диалога:
{history_context if history_context else 'История пока пустая.'}

Новое сообщение пользователя:
{user_text}

Сформируй теплый ответ подруги.
""".strip()

    try:
        model_response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                {"role": "user", "parts": [{"text": system_prompt}]},
                {"role": "user", "parts": [{"text": user_prompt}]},
            ],
        )
        ai_text = (model_response.text or "").strip()
    except Exception as ex:
        err_text = str(ex)
        is_quota = "429" in err_text or "RESOURCE_EXHAUSTED" in err_text

        if ENABLE_LOCAL_FALLBACK and is_quota:
            fallback_messages = build_local_fallback_messages(user_text, period)

            if db_conn is not None:
                try:
                    with db_conn:
                        save_message(db_conn, user_id, "user", user_text)
                        for msg in fallback_messages:
                            save_message(db_conn, user_id, "assistant", msg)
                        upsert_fact_if_detected(db_conn, user_id, user_text)
                except Exception:
                    pass
                finally:
                    db_conn.close()

            return jsonify(
                {
                    "messages": fallback_messages,
                    "meta": {
                        "period": period,
                        "playful_mode": False,
                        "server_time": server_now.isoformat(),
                        "fallback": "local_quota",
                    },
                }
            )

        if db_conn is not None:
            db_conn.close()
        return jsonify({"messages": ["Временный сбой ответа, попробуй через минутку 🤍"]}), 502

    if not ai_text:
        ai_text = "Я рядом, котик 🤍 Расскажи, что у тебя на душе?"

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
