from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import random
import os
import html
import feedparser
from datetime import datetime, timedelta

app = FastAPI(title="CityVibe API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

NEWS_FEEDS = ["https://www.business-gazeta.ru/rss", "https://realnoevremya.ru/rss", "https://www.tatar-inform.ru/rss"]

GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")
GIGACHAT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

# --- ИИ И ПОГОДА ---
async def get_gigachat_token() -> str | None:
    if not GIGACHAT_AUTH_KEY: return None
    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.post(GIGACHAT_AUTH_URL, headers={"Authorization": f"Basic {GIGACHAT_AUTH_KEY}", "RqUID": "00000000-0000-0000-0000-000000000000", "Content-Type": "application/x-www-form-urlencoded"}, data={"scope": "GIGACHAT_API_PERS"})
            if resp.status_code == 200: return resp.json().get("access_token")
        except: pass
    return None

async def ask_gigachat(system_prompt: str, user_prompt: str) -> str | None:
    token = await get_gigachat_token()
    if not token: return None
    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.post(GIGACHAT_API_URL, headers={"Authorization": f"Bearer {token}"}, json={"model": "GigaChat", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], "temperature": 0.7})
            if resp.status_code == 200: return resp.json()["choices"][0]["message"]["content"]
        except: pass
    return None

async def get_weather(city_code: str) -> str:
    city_en = {"kzn": "Kazan", "msk": "Moscow", "spb": "Saint Petersburg", "ekb": "Yekaterinburg", "nsk": "Novosibirsk"}.get(city_code, "Kazan")
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://wttr.in/{city_en}?format=j1")
            if r.status_code == 200:
                curr = r.json()["current_condition"][0]
                return f"{curr['temp_C']}°C, {curr['lang_ru'][0]['value']}"
    except: pass
    return "Погода неизвестна"

# --- СБОР ДАННЫХ ---
def score_event(event: dict, interests: list) -> float:
    score = sum(8.0 for i in interests if i in [c.get("slug", "") for c in event.get("categories", [])])
    if "бесплат" in str(event.get("price", "")).lower(): score += 2.0
    if (event.get("images") or [{}])[0].get("image"): score += 1.0
    return score

async def fetch_events(city: str, days: int) -> list:
    today = datetime.now()
    end_date = today + timedelta(days=days)
    city_name = {"kzn": "Казань", "msk": "Москва", "spb": "Санкт-Петербург", "ekb": "Екатеринбург", "nsk": "Новосибирск"}.get(city.lower(), "Казань")
    token = os.getenv("TIMEPAD_TOKEN", "")
    
    params = {"cities": city_name, "limit": 100, "sort": "starts_at", "fields": "location,poster_image,poster_thumb,categories,description", "starts_at_min": today.strftime("%Y-%m-%dT%H:%M:%S"), "starts_at_max": end_date.strftime("%Y-%m-%dT%H:%M:%S")}
    headers = {"Accept": "application/json"}
    if token: headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=20, headers=headers) as c:
            r = await c.get("https://api.timepad.ru/v1/events", params=params)
            if r.status_code != 200: return []
            data = r.json().get("values", [])
            parsed_events = []
            for e in data:
                start_ts = int(today.timestamp())
                if e.get("starts_at"):
                    try: start_ts = int(datetime.strptime(e["starts_at"][:19], "%Y-%m-%dT%H:%M:%S").timestamp())
                    except: pass
                img_data = e.get("poster_image") or e.get("poster_thumb") or {}
                loc = e.get("location", {})
                parsed_events.append({
                    "id": e.get("id"), "title": html.unescape(e.get("name", "")),
                    "description": html.unescape(e.get("description", "")), # Добавили описание!
                    "dates": [{"start": start_ts, "end": None}],
                    "place": {"title": f"{loc.get('city', '')}, {loc.get('name', '')}".strip(", ")},
                    "price": "Бесплатно" if e.get("is_free") else "Уточнять",
                    "site_url": e.get("url", ""), "images": [{"image": img_data.get("default_url") or img_data.get("big") or ""}],
                    "categories": [{"slug": f"{cat.get('name','').lower()} {cat.get('slug','').lower()}"} for cat in e.get("categories", [])]
                })
            return parsed_events
    except: return []

async def fetch_news() -> list:
    news = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for url in NEWS_FEEDS:
            try:
                r = await c.get(url)
                for e in feedparser.parse(r.text).entries[:10]:
                    news.append({"title": html.unescape(e.get("title", "")), "link": e.get("link", ""), "summary": html.unescape(e.get("summary", ""))[:200]})
            except: continue
    return news if news else [{"title": "Нет новостей", "link": "", "summary": ""}]

def _event_json(e: dict, score: float) -> dict:
    dates = e.get("dates") or []
    first = dates[0] if dates else {}
    return {
        "id": e.get("id"), "title": e.get("title", ""), "description": e.get("description", ""),
        "start": first.get("start"), "place": (e.get("place") or {}).get("title", ""),
        "price": e.get("price", ""), "url": e.get("site_url", ""),
        "image": (e.get("images") or [{}])[0].get("image", ""),
        "categories": [c.get("slug", "") for c in e.get("categories", [])], "score": round(score, 2)
    }

# --- ЭНДПОИНТЫ ---
@app.get("/api/recommendations")
async def recommendations(city: str = Query("kzn"), interests: str = Query(""), days: int = Query(30)):
    ints = [i.strip() for i in interests.split(",") if i.strip()]
    events = await fetch_events(city, days)
    scored = sorted(((e, score_event(e, ints) + random.random()) for e in events), key=lambda x: -x[1])
    return {"city": city, "days": days, "count": len(events), "events": [_event_json(e, s) for e, s in scored[:100]]}

@app.get("/api/smart-digest")
async def smart_digest(city: str = Query("kzn")):
    events = await fetch_events(city, 30)
    news = await fetch_news()
    weather = await get_weather(city)
    
    ev_sorted = sorted(((e, score_event(e, [])) for e in events), key=lambda x: -x[1])[:5]
    
    summary = f"Погода сейчас: {weather}. Рекомендуем обратить внимание на эти события."
    if GIGACHAT_AUTH_KEY:
        prompt = f"Погода в городе: {weather}. Новости: {', '.join([n['title'] for n in news[:3]])}. События: {', '.join([e['title'] for e, _ in ev_sorted])}. Напиши краткий вдохновляющий дайджест (3-4 предложения) с учетом погоды."
        ai_res = await ask_gigachat("Ты городской ассистент.", prompt)
        if ai_res: summary = ai_res

    return {"source": "gigachat" if GIGACHAT_AUTH_KEY else "fallback", "weather": weather, "summary": summary, "top_events": [_event_json(e, s) for e, s in ev_sorted], "top_news": news[:5]}

@app.get("/api/chat")
async def chat_endpoint(query: str = Query(...), city: str = Query("kzn")):
    events = await fetch_events(city, 30)
    events_ctx = ", ".join([e["title"] for e in events[:20]])
    if GIGACHAT_AUTH_KEY:
        prompt = f"Пользователь спрашивает: '{query}'. Вот список доступных событий в городе: {events_ctx}. Ответь на вопрос пользователя, порекомендовав подходящие события из списка, если они есть."
        res = await ask_gigachat("Ты умный помощник по досугу.", prompt)
        if res: return {"answer": res}
    return {"answer": "ИИ временно недоступен. Но вы можете посмотреть афишу!"}
