from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import random
from datetime import datetime

app = FastAPI(title="CityVibe API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def score_event(event: dict, interests: list) -> float:
    score = 0.0
    cats = [c.get("slug", "") for c in event.get("categories", [])]
    score += sum(8.0 for i in interests if i in cats)
    price_text = str(event.get("price", "")).lower()
    if "бесплат" in price_text or not price_text.strip():
        score += 2.0
    return score

async def fetch_events(city: str, days: int) -> list:
    today = datetime.now()
    # Timepad использует числовые ID городов: 14 - Казань, 1 - Москва
    city_map = {"kzn": "14", "msk": "1", "spb": "2"}
    city_id = city_map.get(city, "14")
    
    params = {"cities": city_id, "limit": 5}
    
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.timepad.ru/v1/events", params=params)
            
            # Если Timepad ответил ошибкой (не 200), выводим ее на экран!
            if r.status_code != 200:
                return [{
                    "id": 999, 
                    "title": f"ОШИБКА API {r.status_code}",
                    "dates": [{"start": int(today.timestamp()), "end": None}],
                    "place": {"title": r.text[:150]}, # Текст ошибки
                    "price": "0", "site_url": "", "images": [{"image": ""}], "categories": []
                }]
            
            # Если ответ 200, но событий нет
            data = r.json().get("values", [])
            if len(data) == 0:
                return [{
                    "id": 999, "title": "ОШИБКА: Timepad вернул 0 событий",
                    "dates": [{"start": int(today.timestamp()), "end": None}],
                    "place": {"title": "Пустой ответ"}, "price": "0", "site_url": "", "images": [{"image": ""}], "categories": []
                }]

            # Парсинг (если всё хорошо)
            parsed = []
            for e in data:
                start_str = e.get("starts_at")
                start_ts = int(today.timestamp())
                if start_str:
                    try:
                        start_ts = int(datetime.strptime(start_str[:19], "%Y-%m-%dT%H:%M:%S").timestamp())
                    except: pass
                
                img_data = e.get("poster_image") or e.get("poster_thumb") or {}
                img = img_data.get("default_url") or img_data.get("big") or img_data.get("default") or ""
                
                loc = e.get("location", {})
                place = f"{loc.get('city', '')}, {loc.get('name', '')}".strip(", ")
                
                parsed.append({
                    "id": e.get("id"),
                    "title": e.get("name", ""),
                    "dates": [{"start": start_ts, "end": None}],
                    "place": {"title": place},
                    "price": "Бесплатно" if e.get("is_free") else "Уточнять",
                    "site_url": e.get("url", ""),
                    "images": [{"image": img}],
                    "categories": [{"slug": cat.get("name", "").lower()} for cat in e.get("categories", [])]
                })
            return parsed

    except Exception as ex:
        # Если произошла ошибка в самом Python (например, нет интернета)
        return [{
            "id": 999, "title": f"ОШИБКА PYTHON",
            "dates": [{"start": int(today.timestamp()), "end": None}],
            "place": {"title": str(ex)[:150]}, "price": "0", "site_url": "", "images": [{"image": ""}], "categories": []
        }]

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
async def recommendations(city: str = Query("kzn"), interests: str = Query(""), days: int = Query(14, ge=1, le=30), limit: int = Query(30, ge=1, le=100)):
    ints = [i.strip() for i in interests.split(",") if i.strip()]
    events = await fetch_events(city, days)
    scored = sorted(((e, score_event(e, ints) + random.random()) for e in events), key=lambda x: -x[1])
    return {"city": city, "days": days, "count": len(events), "events": [_event_json(e, s) for e, s in scored[:limit]]}
