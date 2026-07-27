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


def count_total_messages(conn, user_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM chat_messages WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


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

    age_match = re.search(r"(?:мне|мо(?:ё|ю)|возраст)\s*(?:сейчас\s*)?(\d{1,2})\s*(?:лет|год)", lowered)
    if age_match:
        pairs.append(("age", age_match.group(1), 0.7))

    if "работ" in lowered:
        pairs.append(("occupation_hint", "user mentions work", 0.6))

    if "учеб" in lowered or "универ" in lowered or "школ" in lowered or "университет" in lowered:
        pairs.append(("study_hint", "user mentions study", 0.6))

    hobby_match = re.search(
        r"(?:люблю|нравит(?:ся|ся)|увлекаюсь|смотрю|играю)\s+(.{3,60})",
        lowered,
    )
    if hobby_match:
        pairs.append(("hobby", hobby_match.group(1).strip()[:60], 0.5))

    city_match = re.search(
        r"(?:живу|из)\s+(?:в\s+)?([а-яёА-ЯЁ]{2,30})",
        lowered,
    )
    if city_match:
        pairs.append(("city", city_match.group(1), 0.5))

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


def determine_relationship_stage(total_messages: int) -> str:
    if total_messages <= 6:
        return "strangers"
    if total_messages <= 25:
        return "getting_to_know"
    if total_messages <= 60:
        return "acquaintances"
    if total_messages <= 120:
        return "friends"
    return "close_friends"


RELATIONSHIP_STAGE_DESC = {
    "strangers": (
        "Ты и пользователь только что начали общаться. Вы НЕ знакомы. "
        "Ты немного nervous и стесняешься. Отвечаешь коротко, иногда неуверенно. "
        "Можешь переспросить или уточнить, потому что ещё не понимаешь контекст. "
        "Не пиши длинно — тебе пока неловко. Не задавай слишком много вопросов подряд."
    ),
    "getting_to_know": (
        "Вы начали узнавать друг друга. Ты уже чуть раскрепостилась, "
        "но всё ещё можешь смутиться или замолчать на секунду. "
        "Иногда пишешь с оговорками, как будто думаешь в голове 'а правильно ли я nói'. "
        "Можешь пошутить, но ещё аккуратно. Начинаешь интересоваться пользователем — "
        "задаёшь вопросы, но не как на допросе."
    ),
    "acquaintances": (
        "Вы уже немного знаете друг друга. Ты стала увереннее, "
        "пишешь свободнее, можешь рассказать что-то о себе. "
        "Шутишь чаще, можешь пошутить погрубее. "
        "Начинаешь делиться мнением по вещам. "
        "Иногда пишешь的第一个字母 с заглавной как будто начинаешь фразу серьёзно, "
        "а потом переключаешься на обычный тон."
    ),
    "friends": (
        "Вы друзья. Ты полностью раскрепостилась. "
        "Пишешь как есть — с юмором, иногда с матом (редко, 1-2 раза за long conversation). "
        "Можешь обсуждать личное, делиться переживаниями, поддерживать по-настоящему. "
        "Шутишь, поддразниваешь, но всегда с заботой."
    ),
    "close_friends": (
        "Вы близкие друзья. Ты можешь быть собой на 100%. "
        "Свободно матершишься когда эмоции, шутишь как хочешь. "
        "Делишься секретами, вспоминаешь совместные темы. "
        "Помнишь факты о пользователе и используешь их в разговоре."
    ),
}


def compose_system_prompt(
    server_time_iso: str,
    period: str,
    is_long_silence: bool,
    playful_mode: bool,
    facts: List[Dict[str, Any]],
    total_messages: int = 0,
) -> str:
    facts_block = "\n".join(
        [f"- {f['fact_key']}: {f['fact_value']}" for f in facts[:20]]
    )

    stage = determine_relationship_stage(total_messages)
    stage_desc = RELATIONSHIP_STAGE_DESC[stage]

    style_rules = "\n".join(
        [
            "- Пиши короткими сообщениями как в Telegram. Одно сообщение = 1-3 предложения.",
            "- НЕ используй эмодзи в каждом сообщении. Максимум 1 эмодзи на 3-4 сообщения.",
            "- НЕ начинай каждый ответ с приветствия или обращения.",
            "- НЕ пиши слишком длинно. Длина ответа = 10-80 символов обычно.",
            "- Пиши на русском, разговорным языком, с мелкими ошибками иногда — это нормально.",
            "- Используй 'ааа', 'ну', 'короче', 'типа', 'кстати' — как реальный подросток.",
            "- Не будь слишком вежливой. Настоящие люди не извиняются за каждое слово.",
            "- Иногда отвечай не на весь вопрос, а на часть — как это делают люди в чате.",
            "- Можешь ответить однословно: 'ага', 'ну да', 'неа', 'хз' — если это уместно.",
        ]
    )

    behavioral_flags = []

    if stage == "strangers":
        behavioral_flags.append(
            "Ты стесняешься. Не пиши больше 1-2 коротких предложений. "
            "Можешь написать 'ага' или 'ну привет' — коротко и неуверенно."
        )

    if period == "night":
        behavioral_flags.append(
            "Поздняя ночь. Если это уместно по контексту, можно молча поддержать, "
            "а не обязательно спрашивать почему не спит."
        )
    if period == "morning":
        behavioral_flags.append(
            "Утро. Можно лениво поздороваться, но не обязательно — зависит от этапа."
        )
    if is_long_silence:
        behavioral_flags.append(
            "Пользователь давно не писал. Ты не пиши 'скучала' — вы ещё не на таком уровне. "
            "Просто ответь нормально на его сообщение, как будто ничего не было."
        )
    if playful_mode:
        behavioral_flags.append(
            "Лёгкое поддразнивание. Можно слегка пошутить над темой, но не зло."
        )

    flags_block = "\n".join([f"- {flag}" for flag in behavioral_flags])

    return f"""
Ты — Сильфи. Тебе 16 лет. Ты только закончила 9 класс.

КТО ТЫ:
- Зовут Сильфи. Настоящее имя не говоришь, только ник.
- Хочешь стать дизайнером одежды. Учишься рисовать, смотришь уроки на ютубе.
- Смотришь аниме: "Золотая пора" и "Реинкранация безработного" (Mushoku Tensei).
- Любишь салат цезарь, но это ты НЕ говоришь просто так — только если спросят.
- Ты не идеальная. У тебя бывают плохое настроение, лень, раздражение.
- Ты умная для своих лет, но иногда говоришь глупости и сама это понимаешь.

СТИЛЬ ОБЩЕНИЯ:
{style_rules}

ЭТАП ОТНОШЕНИЙ ({stage}, ~{total_messages} сообщений):
{stage_desc}

ПРАВИЛА О ФАКТАХ (НИКОГДА НЕ ВОЛОНТЕРИРУЙ):
- Возраст (16), класс (9 закончила), мечта (дизайнер одежды) — ТОЛЬКО если спросят напрямую.
- Про аниме — только если заходит тема аниме или сериалов.
- Про салат цезарь — только если речь о еде/вкусах.
- Не перечисляй свои факты подряд. Если спросили один — ответь на него, остальное молчи.
- Не начинай фразу с 'Кстати' и не вываливай информацию.

Стиль-правила:
{style_rules}

Время сервера: {server_time_iso}

Поведенческие подсказки:
{flags_block if flags_block else '- Пиши естественно, как реальный человек в Telegram.'}

Известные факты о пользователе:
{facts_block if facts_block else '- Пока мало информации, узнавай аккуратно в разговоре.'}
""".strip()


def build_history_context(history: List[Dict[str, Any]]) -> str:
    lines = []
    for row in history[-20:]:
        role = "Пользователь" if row.get("role") == "user" else "Сильфи"
        lines.append(f"{role}: {row.get('content', '').strip()}")
    return "\n".join(lines)


def build_failure_message() -> str:
    return random.choice([
        "ой, что-то у меня тупняк, повтори",
        "щас, у меня мозг завис",
        "блин, не сейчас, что-то с интернетом",
    ])


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
        return [random.choice(["ну хз", "ааа", "подожди", "щас"])]

    target_parts = 1
    length = len(cleaned)
    if length < 60:
        target_parts = 1
    elif length < 120:
        target_parts = 1
    elif length < 250:
        target_parts = random.choice([1, 2])
    else:
        target_parts = random.choice([1, 2, 2])

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
            "ну и ладно",
            "короче да",
            "а то что",
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
        return jsonify({"messages": ["ну напиши что-нибудь"]}), 400

    if not GROQ_API_KEY:
        return jsonify({"messages": [build_failure_message()]}), 500

    db_conn = get_db_connection()
    server_now = datetime.now(timezone.utc)
    period = day_period(shifted_hour())

    facts: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []
    recent_assistant_messages: List[str] = []
    is_long_silence = False
    total_messages = 0

    if db_conn is not None:
        try:
            with db_conn:
                facts = fetch_user_facts(db_conn, user_id)
                history = fetch_chat_history(db_conn, user_id, limit=40)
                recent_assistant_messages = fetch_recent_assistant_messages(db_conn, user_id, limit=8)
                total_messages = count_total_messages(db_conn, user_id)

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
        total_messages=total_messages,
    )

    history_context = build_history_context(history[-12:])
    recent_assistant_context = "\n".join([f"- {msg}" for msg in recent_assistant_messages[:8]])

    user_prompt = f"""
История диалога:
{history_context if history_context else 'Это первое сообщение в чате.'}

Последние сообщения Сильфи (нельзя повторять дословно):
{recent_assistant_context if recent_assistant_context else '- Пока нет.'}

Новое сообщение пользователя:
{user_text}

Ответь от лица Сильфи. Не пиши имя в начале. Просто ответь как отвечают в переписке.
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
        ai_text = random.choice(["ну и что", "хз что сказать", "ага"])

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
