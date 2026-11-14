from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import random
import os
from datetime import datetime

app = FastAPI(title="CityVibe API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def score_event(event: dict, interests: list) -> float:
    """Оценка события под интересы пользователя."""
    score = 0.0
    cats = [c.get("slug", "") for c in event.get("categories", [])]
    score += sum(8.0 for i in interests if i in cats)
    
    price_text = str(event.get("price", "")).lower()
    if "бесплат" in price_text or not price_text.strip():
        score += 2.0
        
    if (event.get("images") or [{}])[0].get("image"):
        score += 1.0
    return score

async def fetch_events(city: str, days: int) -> list:
    """Запрос к Timepad API с передачей названия города и токена."""
    today = datetime.now()
    
    # Timepad лучше понимает названия городов
    city_map = {"kzn": "Казань", "msk": "Москва", "spb": "Санкт-Петербург"}
    city_name = city_map.get(city, "Казань")
    
    # Берем токен из переменных окружения SpaceWeb
    token = os.getenv("TIMEPAD_TOKEN", "")
    
    # Параметры запроса (ищем только будущие события)
    params = {
        "cities": city_name,
        "limit": 20,
        "sort": "starts_at",
        "fields": "location,poster_image,poster_thumb",
        "starts_at_min": today.strftime("%Y-%m-%dT%H:%M:%S")
    }
    
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("ВНИМАНИЕ: Токен TIMEPAD_TOKEN не найден в переменных окружения!")

    try:
        async with httpx.AsyncClient(timeout=20, headers=headers) as c:
            r = await c.get("https://api.timepad.ru/v1/events", params=params)
            
            # Если ошибка API, показываем её на экране
            if r.status_code != 200:
                print(f"ОШИБКА API {r.status_code}: {r.text}")
                return [{
                    "id": 999, 
                    "title": f"ОШИБКА API {r.status_code}",
                    "dates": [{"start": int(today.timestamp()), "end": None}],
                    "place": {"title": r.text[:200]}, 
                    "price": "0", "site_url": "", "images": [{"image": ""}], "categories": []
                }]
            
            data = r.json().get("values", [])
            
            # Если событий 0, выводим отладочную информацию
            if len(data) == 0:
                return [{
                    "id": 999, "title": "0 событий от Timepad",
                    "dates": [{"start": int(today.timestamp()), "end": None}],
                    "place": {"title": f"Город: {city_name}, Дата: {today.strftime('%Y-%m-%d')}"}, 
                    "price": "0", "site_url": "", "images": [{"image": ""}], "categories": []
                }]

            parsed_events = []
            for e in data:
                # Парсинг даты
                start_str = e.get("starts_at")
                start_ts = int(today.timestamp())
                if start_str:
                    try:
                        clean_str = start_str[:19]
                        start_ts = int(datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S").timestamp())
                    except:
                        pass
                
                # Парсинг картинки
                img_data = e.get("poster_image") or e.get("poster_thumb") or {}
                img = img_data.get("default_url") or img_data.get("big") or img_data.get("default") or ""
                
                # Парсинг цены
                price = "Бесплатно" if e.get("is_free") else ""
                if not price and e.get("ticket_types"):
                    prices = [t.get("price", 0) for t in e.get("ticket_types") if t.get("price")]
                    if prices:
                        price = f"от {min(prices)} руб."
                    else:
                        price = "Уточнять"
                        
                # Парсинг места
                loc = e.get("location", {})
                place_name = loc.get("name", "")
                place_city = loc.get("city", "")
                place = f"{place_city}, {place_name}" if place_name else place_city
                
                # Категории
                cats = [{"slug": cat.get("name", "").lower()} for cat in e.get("categories", [])]
                
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

    except Exception as ex:
        print(f"ОШИБКА PYTHON: {ex}")
        return [{
            "id": 999, "title": "ОШИБКА СЕТИ",
            "dates": [{"start": int(today.timestamp()), "end": None}],
            "place": {"title": str(ex)[:200]}, 
            "price": "0", "site_url": "", "images": [{"image": ""}], "categories": []
        }]

def _event_json(e: dict, score: float) -> dict:
    """Форматирование события для отправки в мобильное приложение."""
    dates = e.get("dates") or []
    first = dates[0] if dates else {}
    return {
        "id": e.get("id"),
        "title": e.get("title", ""),
        "start": first.get("start"),
        "end": first.get("end"),
        "place": (e.get("place") or {}).get("title", ""),
        "price": e.get("price", ""),
        "url": e.get("site_url", ""),
        "image": (e.get("images") or [{}])[0].get("image", ""),
        "categories": [c.get("slug", "") for c in e.get("categories", [])],
        "score": round(score, 2),
    }

@app.get("/api/recommendations")
async def recommendations(
    city: str = Query("kzn"),
    interests: str = Query(""),
    days: int = Query(14, ge=1, le=30),
    limit: int = Query(30, ge=1, le=100),
):
    """Главный эндпоинт, который вызывает мобильное приложение."""
    ints = [i.strip() for i in interests.split(",") if i.strip()]
    events = await fetch_events(city, days)
    
    scored = sorted(
        ((e, score_event(e, ints) + random.random()) for e in events),
        key=lambda x: -x[1],
    )
    
    return {
        "city": city,
        "days": days,
        "count": len(events),
        "events": [_event_json(e, s) for e, s in scored[:limit]],
    }
