import json
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import feedparser

# ==========================================
# 1. НАСТРОЙКИ
# ==========================================
KUDA_GO = "https://kudago.com/public-api/v1.4"
PROVIDER = os.getenv("LLM_PROVIDER", "none") # gigachat | openai | yandex | none

NEWS_FEEDS = [
    "https://www.business-gazeta.ru/rss",
    "https://realnoevremya.ru/rss",
    "https://www.tatar-inform.ru/rss",
]

NEWS_KEYWORDS = {
    "concert":     ["концерт", "музык", "выступлен", "группа"],
    "theater":     ["театр", "спектакл", "премьер", "постановк"],
    "exhibition":  ["выставк", "экспозиц", "музе", "художник"],
    "cinema":      ["кинопремьер", "фильм", "режиссёр", "показ"],
    "festival":    ["фестивал", "праздник", "юбилей"],
    "sport":       ["матч", "турнир", "спортив", "чемпионат"],
    "education":   ["лекци", "курс", "обучен", "семинар"],
    "kids":        ["детск", "для детей", "школьник"],
    "tour":        ["экскурс", "прогулк", "тур"],
}

SYSTEM_PROMPT = """Ты — городской ассистент CityVibe. Анализируешь события и новости,
подбираешь пользователю лучшие варианты досуга. Отвечай строго в формате JSON."""

# ==========================================
# 2. ЛОГИКА СКОРИНГА (Оценка событий)
# ==========================================
def score_event(event: dict, interests: list) -> float:
    score = 0.0
    cats = [c.get("slug", "") for c in event.get("categories", [])]
    score += sum(8.0 for i in interests if i in cats)
    price_text = str(event.get("price", "")).lower()
    if "бесплат" in price_text or not price_text.strip():
        score += 2.0
    if (event.get("images") or [{}])[0].get("image"):
        score += 1.0
    return score

def score_news(item: dict, interests: list) -> float:
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    score = 0.0
    for interest in interests:
        for kw in NEWS_KEYWORDS.get(interest, []):
            if kw in text:
                score += 3.0
                break
    return score

# ==========================================
# 3. ЛОГИКА LLM (AI-анализ)
# ==========================================
async def _openai_chat(system: str, user: str) -> Optional[str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key: return None
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.3},
        )
        return r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else None

async def _gigachat_chat(system: str, user: str) -> Optional[str]:
    auth_key = os.getenv("GIGACHAT_AUTH_KEY")
    if not auth_key: return None
    async with httpx.AsyncClient(timeout=30, verify=False) as c:
        tok = await c.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={"Authorization": f"Basic {auth_key}", "RqUID": "00000000-0000-0000-0000-000000000000", "Content-Type": "application/x-www-form-urlencoded"},
            data={"scope": "GIGACHAT_API_PERS"},
        )
        if tok.status_code != 200: return None
        access = tok.json()["access_token"]
        r = await c.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {access}"},
            json={"model": "GigaChat", "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.3},
        )
        return r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else None

async def _yandex_chat(system: str, user: str) -> Optional[str]:
    api_key, folder = os.getenv("YANDEX_API_KEY"), os.getenv("YANDEX_FOLDER_ID")
    if not api_key or not folder: return None
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": f"Api-Key {api_key}"},
            json={"modelUri": f"gpt://{folder}/yandexgpt-lite", "completionOptions": {"temperature": 0.3, "maxTokens": 2000}, "messages": [{"role": "system", "text": system}, {"role": "user", "text": user}]},
        )
        return r.json()["result"]["alternatives"][0]["message"]["text"] if r.status_code == 200 else None

async def llm_json(system: str, user: str) -> Optional[dict]:
    if PROVIDER == "none": return None
    try:
        if PROVIDER == "openai": raw = await _openai_chat(system, user)
        elif PROVIDER == "gigachat": raw = await _gigachat_chat(system, user)
        elif PROVIDER == "yandex": raw = await _yandex_chat(system, user)
        else: return None
        if not raw: return None
        raw = raw.strip().strip("`")
        if raw.startswith("json"): raw = raw[4:].strip()
        return json.loads(raw)
    except Exception:
        return None

# ==========================================
# 4. API И ЗАПРОСЫ К ИНТЕРНЕТУ
# ==========================================
app = FastAPI(title="CityVibe API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

async def fetch_events(city: str, days: int) -> list:
    today = datetime.now()
    
    # ID городов в Timepad: 1 - Москва, 2 - СПб, 14 - Казань
    city_ids = {"kzn": "14", "msk": "1", "spb": "2"}
    city_id = city_ids.get(city, "14")
    
    params = {
        "cities": city_id,
        "limit": 40,
        "sort": "starts_at",
        "starts_at_min": today.strftime("%Y-%m-%dT%H:%M:%S"), # Только будущие события
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    
    async with httpx.AsyncClient(timeout=20, headers=headers) as c:
        try:
            r = await c.get("https://api.timepad.ru/v1/events", params=params)
            # Выводим статус в логи, чтобы мы могли проверить
            print(f"Timepad status: {r.status_code}")
            
            if r.status_code == 200:
                data = r.json().get("values", [])
                print(f"УСПЕХ: Timepad вернул {len(data)} событий!")
                
                parsed_events = []
                for e in data:
                    start_str = e.get("starts_at")
                    start_ts = int(today.timestamp()) + 86400
                    if start_str:
                        try:
                            clean_str = start_str[:19]
                            start_ts = int(datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S").timestamp())
                        except:
                            pass
                    
                    img_data = e.get("poster_thumb") or {}
                    img = img_data.get("big") or img_data.get("default") or ""
                    
                    price = "Бесплатно" if e.get("is_free") else ""
                    if not price and e.get("ticket_types"):
                        prices = [t.get("price", 0) for t in e.get("ticket_types") if t.get("price")]
                        if prices:
                            price = f"от {min(prices)} руб."
                        else:
                            price = "Уточнять"
                            
                    cats = [{"slug": cat.get("name", "").lower()} for cat in e.get("categories", [])]
                    
                    loc = e.get("location", {})
                    place_name = loc.get("name", "")
                    place_city = loc.get("city", "")
                    place = f"{place_city}, {place_name}" if place_name else place_city
                    
                    parsed_events.append({
                        "id": e.get("id"),
                        "title": e.get("name", ""),
                        "dates": [{"start": start_ts, "end": None}],
                        "place": {"title": place},
                        "price": price,
                        "site_url": e.get("url", ""),
                        "images": [{"image": img}],
                        "categories": cats
                    })
                return parsed_events
            else:
                print(f"ОШИБКА Timepad: {r.text}")
        except Exception as ex:
            print(f"ОШИБКА СЕТИ: {ex}")

    # Демо-режим (сработает, если Timepad недоступен)
    return [
        {
            "id": 1001,
            "title": "Тестовый концерт (Демо-режим)",
            "dates": [{"start": int(today.timestamp()) + 86400, "end": None}],
            "place": {"title": "Казань, Кремль"},
            "price": "Бесплатно",
            "site_url": "https://kudago.com",
            "images": [{"image": "https://picsum.photos/400/200?random=1"}],
            "categories": [{"slug": "concert"}]
        }
    ]
def _event_json(e: dict, score: float) -> dict:
    dates = e.get("dates") or []
    first = dates[0] if dates else {}
    return {"id": e.get("id"), "title": e.get("title", ""), "start": first.get("start"), "end": first.get("end"), "place": (e.get("place") or {}).get("title", ""), "price": e.get("price", ""), "url": e.get("site_url", ""), "image": (e.get("images") or [{}])[0].get("image", ""), "categories": [c.get("slug", "") for c in e.get("categories", [])], "score": round(score, 2)}

# ==========================================
# 5. ЭНДПОИНТЫ (Маршруты)
# ==========================================
@app.get("/api/recommendations")
async def recommendations(city: str = Query("kzn"), interests: str = Query(""), days: int = Query(14, ge=1, le=30), limit: int = Query(30, ge=1, le=100)):
    ints = [i.strip() for i in interests.split(",") if i.strip()]
    events = await fetch_events(city, days)
    scored = sorted(((e, score_event(e, ints) + random.random()) for e in events), key=lambda x: -x[1])
    return {"city": city, "days": days, "count": len(events), "events": [_event_json(e, s) for e, s in scored[:limit]]}

@app.get("/api/smart-digest")
async def smart_digest(city: str = Query("kzn"), interests: str = Query(""), days: int = Query(14, ge=1, le=30)):
    ints = [i.strip() for i in interests.split(",") if i.strip()]
    events = await fetch_events(city, days)
    news_items = await fetch_news()
    ev_sorted = sorted(((e, score_event(e, ints)) for e in events), key=lambda x: -x[1])[:40]
    news_sorted = sorted(((n, score_news(n, ints)) for n in news_items), key=lambda x: -x[1])[:30]

    ev_ctx = [{"id": e["id"], "title": e["title"], "place": (e.get("place") or {}).get("title", ""), "price": e.get("price", ""), "categories": [c.get("slug") for c in e.get("categories", [])]} for e, _ in ev_sorted]
    news_ctx = [{"title": n["title"], "summary": n.get("summary", "")[:200]} for n, _ in news_sorted]

    user_prompt = f"Интересы: {ints or 'не указаны'}. Дней: {days}.\nСобытия (JSON):\n{json.dumps(ev_ctx, ensure_ascii=False)}\nНовости (JSON):\n{json.dumps(news_ctx, ensure_ascii=False)}\nВерни JSON: {{\"top_event_ids\": [], \"top_news_indices\": [], \"summary\": \"\", \"recommendation\": \"\"}}"

    llm_result = await llm_json(SYSTEM_PROMPT, user_prompt)

    if llm_result:
        id_to_event = {e["id"]: (e, s) for e, s in ev_sorted}
        top_events = [_event_json(*id_to_event[eid]) for eid in llm_result.get("top_event_ids", [])[:5] if eid in id_to_event]
        top_news = [{**news_sorted[i][0], "score": round(news_sorted[i][1], 2)} for i in llm_result.get("top_news_indices", [])[:5] if isinstance(i, int) and 0 <= i < len(news_sorted)]
        return {"source": "llm", "period_days": days, "summary": llm_result.get("summary", ""), "recommendation": llm_result.get("recommendation", ""), "top_events": top_events, "top_news": top_news}

    return {"source": "fallback", "period_days": days, "summary": "AI-анализ недоступен.", "recommendation": "", "top_events": [_event_json(e, s) for e, s in ev_sorted[:5]], "top_news": [{**n, "score": round(s, 2)} for n, s in news_sorted[:5]]}
