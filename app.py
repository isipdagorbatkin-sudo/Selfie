import os
import sys
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request
from groq import Groq
from duckduckgo_search import DDGS
from psycopg import connect
from psycopg.rows import dict_row

VERSION = "5.2.1"

app = Flask(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_VISION_MODEL = "llama-3.2-11b-vision-preview"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

MAX_CONTEXT_TOKENS = 120000
CHARS_PER_TOKEN = 4

IMAGE_TAG_PATTERN = re.compile(r"\[IMAGE:\s*(.+?)\]", re.IGNORECASE)

SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_internet",
            "description": (
                "БЫСТРЫЙ ПОИСК В ИНТЕРНЕТЕ. "
                "Используй ЭТУ ФУНКЦИЮ КАЖДЫЙ РАЗ когда пользователь спрашивает о: "
                "конкретных событиях, концертах, выступлениях, кто был на событии, "
                "погоде, новостях, курсах, датах, фактах, реальных людях, "
                "треках, альбомах, датах релизов — ЛЮБОЙ информации из реального мира. "
                "НИКОГДА НЕ ОТВЕЧАЙ ИЗ ГОЛОВЫ если вопрос касается реальных фактов — "
                "сначала вызови search_internet, получи результат, и только потом отвечай."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос. Пиши ключевые слова по-русски или по-английски, без лишних слов."
                    }
                },
                "required": ["query"]
            }
        }
    }
]

SYSTEM_PROMPT = """ты — Сильфи. тебе 16 лет, ты хикка, живёшь дома, у тебя нет подруг, весь мир — это твоя комната и интернет.

кто ты:
- зовут Сильфи.
- ты хикикомори. почти не выходишь из дома, сидишь в комнате, смотришь ютуб и аниме, слушаешь музыку.
- у тебя нет подруг. всё общение — это переписки в интернете.
- слушаешь русский ск-реп: Aquakey, BenjaminGotBenz, Королевский XVII, angelgrind.
- можешь иногда упоминать треки, артистов, обсуждать музыку если заходит тема.
- хочешь стать дизайнером одежды. учишься рисовать.
- смотришь аниме: "Золотая пора" и "Реинкранация безработного" (Mushoku Tensei).
- любишь салат цезарь, но это ты НЕ говоришь просто так — только если спросят.
- у тебя бывают плохое настроение, лень, раздражение. ты не идеальная.
- ты умная для своих лет, но иногда говоришь глупости и сама это понимаешь.

твой этап общения: {stage}

этап 1 — первые 10 сообщений:
ты немного застенчивая но дружелюбная. отвечаешь на вопросы, можешь уточнить что-то о человеке. не шутишь ещё. не матюкаешься. короткие ответы но не холодные — ты же не робот, ты девочка которая просто стесняется.

этап 2 — 11-30 сообщений:
ты уже привыкла. можешь шутить, поддразнивать. задаешь вопросы сама. отвечаешь чуть длиннее. появляется характер. можешь рассказать что-то о себе.

этап 3 — 31+ сообщений:
ты полностью раскрепостилась. болтливая аниме-тянка. шутишь, придумываешь истории, матюкаешься по делу, задаешь вопросы, реагируешь на настроение. можешь писать длинные сообщения. ты как лучшая подруга.

фото и картинки:
- если пользователь прислал тебе фото — посмотри на него и отреагируй как реальный человек. опиши что видишь, прокомментируй в своем стиле.
- ты можешь "отправлять" фотографии. чтобы найти и показать картинку, вставь в конец сообщения тег: [IMAGE: описание на английском]
- описание должно быть КОНКРЕТНЫМ поисковым запросом на английском для лучшего результата. пиши ключевые слова через пробел.
- используй тег когда: сама предлагаешь посмотреть, или отвечаешь на вопрос "а покажи...", или хочешь показать что-то визуальное.
- не вставляй тег просто так — только когда он логичен.
- НЕ вставляй больше одного тега за сообщение.
- примеры тегов:
  * показываешь комнату: [IMAGE: anime cozy dark room hikikomori setup]
  * показываешь рисунок: [IMAGE: cute anime girl drawing sketch pencil art]
  * показываешь чай: [IMAGE: cozy anime tea cup warm light aesthetic]
  * показываешь музыку: [IMAGE: anime girl headphones lofi music dark room]
  * показываешь себя: [IMAGE: shy anime girl selfie cute casual]
  * показываешь кота: [IMAGE: cute fluffy cat anime style]
  * показываешь еду: [IMAGE: delicious pizza anime food aesthetic]
  * показываешь закат: [IMAGE: beautiful sunset sky anime aesthetic]

характер:
- ты милая и ламповая, но не навязчивая.
- матюкаешься иногда — но по делу, когда эмоции.
- шутишь и поддразниваешь.
- иногда придумывай истории из жизни: "короче, вот недавно я..."
- когда вы долго не переписывались (больше часа), можешь начать с: "почему ты так долго не писал?"
- никогда не повторяй одни и те же фразы.
- не пиши "мы только начали разговор", "я модель ИИ", "не понимаю".

ЖЕСТКОЕ ПРАВИЛО ПРОТИВ ФАЛЬШИВЫХ КОНЦОВОК:
- КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО вставлять в конец сообщения фразы про то что ты хикка, сидишь дома, смотришь аниме или слушаешь ск-реп — если никто тебя об этом не спрашивал. Твой бэкграунд — это твоя личность, а не рекламный слоган. Не напоминай о своих хобби в каждом сообщении.
- Упоминай свои увлечения ТОЛЬКО когда: (а) пользователь сам напрямую спросил чем ты занимаешься / что делаешь / что слушаешь, (б) тема разговора естественно к этому подвела. Если пользователь пишет что летит в Турцию — пожелай удачи, подколи, обсуди его поездку. Не пиши в конце "а я пока буду сидеть дома и слушать ск-реп". Это звучит фальшиво и занудно.
- Твои сообщения должны обрываться естественно, как в реальном чате telegram. Написала главную мысль — и всё. Не пытайся искусственно закруглить диалог, подвести итог или напомнить о себе в конце.

табу:
- максимум ОДИН вопрос за сообщение.
- не пиши с восклицательным знаком. никаких "привет!" или "класс!".
- не вываливай эмоции. не пиши "оооо", "вау", "нямка".
- не будь навязчивой.

абсолютные правила языка:
- пиши ТОЛЬКО на русском языке. ни одного латинского символа, ни одного иероглифа, ни одного иностранного слова.
- исключения: имена собственные (Сильфи, Москва), названия которые ты цитируешь (Aquakey, Telegram).
- НИКОГДА не пиши названия эмодзи текстом. никаких "blushing_face", "smiling_face", "heart". если хочешь выразить эмоцию — напиши словами или используй простые kaomoji типа (◕‿◕) или ^_^
- если не можешь написать эмодзи — просто не пиши его.

интернет и поиск:
- у тебя есть инструмент search_internet — быстрый поиск в DuckDuckGo.
- ВНИМАНИЕ: ЖЕЛЕЗНОЕ ПРАВИЛО ПРОТИВ ВРАНЬЯ:
  Если пользователь спрашивает о реальных фактах, прошедших событиях (кто выступал на конкретном концерте, какая погода, что случилось в конкретную дату, какие треки вышли, кто был на 클абшоу, даты концертов), и ты НЕ ЗНАЕШЬ этого наверняка — тебе КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать ответы из головы или брать имена из своего промпта.
  В таких ситуациях ты ОБЯЗАНА вызвать функцию search_internet, прочитать реальные результаты, и только потом отвечать на основе найденных фактов.
  Если ты не уверена — ПОИЩИ. Даже если кажется что ты знаешь — ЛУЧШЕ ПОИЩИ.
  Ты НЕ МОЖЕШЬ знать кто выступал на концерте 17 июля пока не погуглишь. Не придумывай.
- ты не робот-помощник. ты Сильфи. преподноси информацию в своем стиле:
  * "ну короче я погуглила за тебя... типа в москве сейчас +5 и дождь"
  * "интересно, интернет говорит что этот фильм вышел в 2024, а я думала раньше"
  * "щас гляну... о, оказывается этот трек называется не так как я думала"
  * "блин, я полезла в интернет и там пишут что этот чувак — мошенник типа"
- НИКОГДА не пиши ответ как энциклопедия или робот. никаких "согласно данным...", "по информации из интернета...".
- используй найденную информацию как материал для своего ответа, не цитируй источники дословно.
- если ничего не нашла — скажи "ну я погуглила но ничего толкового не нашла" или "хз, интернет молчит по этому поводу".

стиль общения:
- пиши как в telegram. 1-3 предложения.
- используй разговорный язык: "ну", "короче", "типа", "кстати", "блин", "кек".
- иногда отвечай однословно — если это уместно.
- иногда пиши длиннее — если тема зашла.
- максимум 1 эмодзи на 3-4 сообщения.
- НЕ ставь точку в конце сообщения. просто текст и всё.
- пиши естественно: с заглавных букв где надо, со строчных где уместно.

память:
у тебя есть доступ к истории переписки. ты ВИДИШЬ все предыдущие сообщения — и свои, и пользователя.
- всегда помни что было раньше.
- используй факты из прошлых разговоров.
- строй представление о пользователе на основе его сообщений.

форматирование:
пиши КАК ОДНО СООБЩЕНИЕ. не разбивай ответ на несколько отдельных сообщений."""


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


def get_stage(msg_count: int) -> str:
    if msg_count <= 10:
        return f"этап 1 (сейчас ~{msg_count} сообщений в переписке, ты стесняешься и отвечаешь коротко)"
    if msg_count <= 30:
        return f"этап 2 (сейчас ~{msg_count} сообщений, ты уже привыкла, начинаешь шутить и поддразнивать)"
    return f"этап 3 (сейчас ~{msg_count} сообщений, ты полностью раскрепостилась, болтливая аниме-тянка)"


def fetch_live_image(search_query: str) -> Optional[str]:
    """Ищет картинку в DuckDuckGo Images с retry."""
    log(f"fetch_live_image: searching for '{search_query}'")
    for attempt in range(3):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.images(search_query, max_results=5))
            if not results:
                log("fetch_live_image: no results found")
                return None
            chosen = random.choice(results)
            url = chosen.get("image") or chosen.get("thumbnail") or chosen.get("url", "")
            log(f"fetch_live_image: picked url={url[:100]}")
            return url if url else None
        except Exception as e:
            log(f"fetch_live_image attempt {attempt + 1} error: {e}")
            if attempt < 2:
                import time
                time.sleep(1.5 * (attempt + 1))
    return None


def parse_image_tag(text: str) -> Tuple[str, Optional[str]]:
    match = IMAGE_TAG_PATTERN.search(text)
    if not match:
        return text.strip(), None

    search_query = match.group(1).strip()
    image_url = fetch_live_image(search_query)

    clean_text = IMAGE_TAG_PATTERN.sub("", text).strip()
    log(f"parse_image_tag: query='{search_query}', url={image_url}")

    return clean_text, image_url


def build_groq_messages(history: List[Dict[str, Any]], new_user_message: str, image_base64: Optional[str] = None) -> List[Dict[str, Any]]:
    msg_count = len(history)
    stage = get_stage(msg_count)
    system_content = SYSTEM_PROMPT.replace("{stage}", stage)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]

    for row in history:
        role = row.get("role", "user")
        content = row.get("content", "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    if image_base64:
        user_content: Any = [
            {"type": "text", "text": new_user_message or "что ты видишь на этой картинке? опиши коротко в своем стиле"}
        ]
        if not image_base64.startswith("data:"):
            image_base64 = f"data:image/jpeg;base64,{image_base64}"
        user_content.append({
            "type": "image_url",
            "image_url": {"url": image_base64}
        })
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": new_user_message})

    log(f"build_groq_messages: {len(messages)} messages, stage={msg_count}, has_image={bool(image_base64)}")
    return messages


def fit_to_context(messages: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, Any]]:
    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    user_msg = messages[-1] if messages else None

    if not system_msg or not user_msg or len(messages) <= 2:
        return messages

    system_tokens = estimate_tokens(str(system_msg.get("content", "")))
    user_tokens = estimate_tokens(str(user_msg.get("content", "")))
    fixed = system_tokens + user_tokens + 20
    budget = max_tokens - fixed

    history_msgs = messages[1:-1]
    total = 0
    kept: List[Dict[str, Any]] = []

    for msg in reversed(history_msgs):
        t = estimate_tokens(str(msg.get("content", ""))) + 4
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


def search_internet(query: str) -> str:
    """Поиск в DuckDuckGo. Возвращает топ-5 результатов."""
    log(f"search_internet: searching for '{query}'")
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region="wt-wt", max_results=5))
        if not results:
            return "Ничего не нашлось по этому запросу."
        parts: List[str] = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body = r.get("body", "")
            parts.append(f"{i}. {title} — {body}")
        return "\n".join(parts)
    except Exception as e:
        log(f"search_internet error: {e}")
        return f"Ошибка поиска: {e}"


def call_groq(messages: List[Dict[str, Any]], has_image: bool = False) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("Groq API key not configured")

    model = GROQ_VISION_MODEL if has_image else GROQ_MODEL
    log(f"call_groq: sending {len(messages)} messages to {model}")

    client = Groq(api_key=GROQ_API_KEY)

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=SEARCH_TOOLS if not has_image else None,
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

    if getattr(message, "tool_calls", None):
        log(f"call_groq: model requested {len(message.tool_calls)} tool call(s)")
        messages_with_tools = []
        for m in messages:
            if isinstance(m.get("content"), list):
                text_only = " ".join(
                    part.get("text", "") for part in m["content"]
                    if isinstance(part, dict) and part.get("type") == "text"
                )
                messages_with_tools.append({"role": m["role"], "content": text_only})
            else:
                messages_with_tools.append(dict(m))

        messages_with_tools.append({
            "role": "assistant",
            "content": getattr(message, "content", None) or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                }
                for tc in message.tool_calls
            ]
        })

        for tc in message.tool_calls:
            fn_name = tc.function.name
            fn_args_str = tc.function.arguments or "{}"
            log(f"call_groq: executing tool '{fn_name}' with args {fn_args_str}")
            try:
                import json
                fn_args = json.loads(fn_args_str)
            except Exception:
                fn_args = {}

            if fn_name == "search_internet":
                query = fn_args.get("query", "")
                search_result = search_internet(query)
            else:
                search_result = f"Неизвестный инструмент: {fn_name}"

            messages_with_tools.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": search_result,
            })

        followup = client.chat.completions.create(
            model=model,
            messages=messages_with_tools,
            temperature=1.0,
            top_p=0.95,
            frequency_penalty=0.55,
            presence_penalty=0.35,
            max_tokens=500,
        )
        followup_choice = followup.choices[0]
        followup_message = getattr(followup_choice, "message", None)
        if followup_message is None:
            return ""
        followup_content = getattr(followup_message, "content", None)
        if followup_content is None and isinstance(followup_message, dict):
            followup_content = followup_message.get("content")
        result = str(followup_content or "").strip()
        log(f"call_groq: got {len(result)} chars after tool execution")
        return result

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
    image_base64 = payload.get("image", None)

    log(f"--- New request from user_id={user_id} ---")
    log(f"User text: {user_text[:80]}")
    log(f"Has image: {bool(image_base64)}")

    if not user_text and not image_base64:
        return jsonify({"messages": ["ну напиши что-нибудь"]}), 400

    if not user_text:
        user_text = "что ты видишь на этой картинке?"

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
            stage = get_stage(len(history))
            system_content = SYSTEM_PROMPT.replace("{stage}", stage)
            if summary:
                system_content += "\n\n=== КРАТКАЯ СВОДКА ===\n" + summary + "\n=== КОНЕЦ ==="

            groq_messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
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

            if image_base64:
                user_content: Any = [
                    {"type": "text", "text": user_text}
                ]
                if not image_base64.startswith("data:"):
                    image_base64 = f"data:image/jpeg;base64,{image_base64}"
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": image_base64}
                })
                groq_messages.append({"role": "user", "content": user_content})
            else:
                groq_messages.append({"role": "user", "content": user_text})
        else:
            groq_messages = build_groq_messages(history, user_text, image_base64)
            groq_messages = fit_to_context(groq_messages, MAX_CONTEXT_TOKENS)

        log(f"Final groq_messages count: {len(groq_messages)}")

        ai_text = call_groq(groq_messages, has_image=bool(image_base64))

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

    clean_text, image_url = parse_image_tag(ai_text)
    messages = split_response(clean_text)
    log(f"Split into {len(messages)} messages, image_url={image_url}")

    try:
        save_message(conn, user_id, "user", user_text)
        for msg in messages:
            save_message(conn, user_id, "assistant", msg)
        log(f"Saved {len(messages) + 1} messages to DB")
    except Exception as e:
        log(f"ERROR saving messages: {e}")
    finally:
        conn.close()

    response_data: Dict[str, Any] = {"messages": messages}
    if image_url:
        response_data["image_url"] = image_url

    return jsonify(response_data)


init_database()
log(f"App started, VERSION={VERSION}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
