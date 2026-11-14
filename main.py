from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import random
import os
import html
import json
import feedparser
from datetime import datetime, timedelta

app = FastAPI(title="CityVibe API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

NEWS_FEEDS = [
    "https://www.business-gazeta.ru/rss",
    "https://realnoevremya.ru/rss",
    "https://www.tatar-inform.ru/rss",
]

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
    keywords = ["концерт", "театр", "выставк", "кино", "фестивал", "спорт", "лекци"]
    for kw in keywords:
        if kw in text: score += 3.0
    return score

async def fetch_events(city: str, days: int) -> list:
    today = datetime.now()
    end_date = today + timedelta(days=days)
    
    # Словарь популярных городов
    city_map = {
        "kzn": "Казань", "msk": "Москва", "spb": "Санкт-Петербург",
        "ekb": "Екатеринбург", "nsk": "Новосибирск", "nnv": "Нижний Новгород",
        "sam": "Самара", "ufa": "Уфа", "krd": "Краснодар", "sochi": "Сочи"
    }
    city_name = city_map.get(city.lower(), city.title())
    
    token = os.getenv("TIMEPAD_TOKEN", "")
    
    params = {
        "cities": city_name,
        "limit": 50, # Берем больше событий
        "sort": "starts_at",
        "fields": "location,poster_image,poster_thumb,categories",
        "starts_at_min": today.strftime("%Y-%m-%dT%H:%M:%S"),
        "starts_at_max": end_date.strftime("%Y-%m-%dT%H:%M:%S"), # Фильтр по дате окончания
    }
    
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=20, headers=headers) as c:
            r = await c.get("https://api.timepad.ru/v1/events", params=params)
            if r.status_code != 200: return []
            data = r.json().get("values", [])
            
            parsed_events = []
            for e in data:
                clean_title = html.unescape(e.get("name", ""))
                start_str = e.get("starts_at")
                start_ts = int(today.timestamp())
                if start_str:
                    try:
                        start_ts = int(datetime.strptime(start_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp())
                    except: pass
                
                img_data = e.get("poster_image") or e.get("poster_thumb") or {}
                img = img_data.get("default_url") or img_data.get("big") or ""
                
                price = "Бесплатно" if e.get("is_free") else "Уточнять"
                loc = e.get("location", {})
                place = f"{loc.get('city', '')}, {loc.get('name', '')}".strip(", ")
                
                # Получаем категории и переводим в нижний регистр
                cats = [cat.get("name", "").lower() for cat in e.get("categories", [])]
                
                parsed_events.append({
                    "id": e.get("id"), "title": clean_title,
                    "dates": [{"start": start_ts, "end": None}],
                    "place": {"title": place}, "price": price,
                    "site_url": e.get("url", ""), "images": [{"image": img}],
                    "categories": [{"slug": c} for c in cats]
                })
            return parsed_events
    except Exception as ex:
        print(f"ОШИБКА: {ex}")
        return []

async def fetch_news() -> list:
    news = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for url in NEWS_FEEDS:
            try:
                r = await c.get(url)
                feed = feedparser.parse(r.text)
                for e in feed.entries[:10]:
                    news.append({
                        "title": html.unescape(e.get("title", "")),
                        "link": e.get("link", ""),
                        "summary": html.unescape(e.get("summary", ""))[:200],
                    })
            except: continue
    return news

def _event_json(e: dict, score: float) -> dict:
    dates = e.get("dates") or []
    first = dates[0] if dates else {}
    return {
        "id": e.get("id"), "title": e.get("title", ""),
        "start": first.get("start"), "end": first.get("end"),
        "place": (e.get("place") or {}).get("title", ""),
        "price": e.get("price", ""), "url": e.get("site_url", ""),
        "image": (e.get("images") or [{}])[0].get("image", ""),
        "categories": [c.get("slug", "") for c in e.get("categories", [])],
        "score": round(score, 2),
    }

@app.get("/api/recommendations")
async def recommendations(
    city: str = Query("kzn"), interests: str = Query(""),
    days: int = Query(30, ge=1, le=90), limit: int = Query(50, ge=1, le=100)
):
    ints = [i.strip() for i in interests.split(",") if i.strip()]
    events = await fetch_events(city, days)
    scored = sorted(((e, score_event(e, ints) + random.random()) for e in events), key=lambda x: -x[1])
    return {"city": city, "days": days, "count": len(events), "events": [_event_json(e, s) for e, s in scored[:limit]]}

@app.get("/api/smart-digest")
async def smart_digest(city: str = Query("kzn")):
    events = await fetch_events(city, 30)
    news_items = await fetch_news()

    ev_sorted = sorted(((e, score_event(e, [])) for e in events), key=lambda x: -x[1])[:5]
    news_sorted = sorted(((n, score_news(n, [])) for n in news_items), key=lambda x: -x[1])[:5]

    return {
        "source": "smart-fallback",
        "summary": "Мы проанализировали городские новости и афишу. Вот что сейчас происходит в городе.",
        "recommendation": "Рекомендуем обратить внимание на эти события и новости. Они лучше всего подходят для отличного отдыха.",
        "top_events": [_event_json(e, s) for e, s in ev_sorted],
        "top_news": [{**n, "score": round(s, 2)} for n, s in news_sorted],
    }
