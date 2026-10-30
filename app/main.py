import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import engine, Base, get_db
from app.models import User, Event, EventStatus, PlatformType, Registration, RegistrationStatus
from app.schemas import (
    UserCreateSchema,
    UserResponseSchema,
    EventCreateSchema,
    EventResponseSchema,
    RegistrationCreateSchema,
)
from app import crud
from app.email_service import send_confirmation_email
from app import tg_bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "static" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


class AdminLoginRequest(BaseModel):
    password: str


async def resolve_address_coordinates(address: str) -> tuple[float, float] | None:
    clean_addr = address.strip()
    if "тюмень" not in clean_addr.lower():
        clean_addr = f"г. Тюмень, {clean_addr}"

    if settings.YANDEX_MAPS_API_KEY and settings.YANDEX_MAPS_API_KEY != "your_yandex_maps_js_api_key":
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(
                    "https://geocode-maps.yandex.ru/1.x/",
                    params={
                        "apikey": settings.YANDEX_MAPS_API_KEY,
                        "geocode": clean_addr,
                        "format": "json",
                        "results": 1,
                    },
                )
                if resp.status_code == 200:
                    members = (
                        resp.json()
                        .get("response", {})
                        .get("GeoObjectCollection", {})
                        .get("featureMember", [])
                    )
                    if members:
                        pos = members[0]["GeoObject"]["Point"]["pos"]
                        lon_str, lat_str = pos.split()
                        return float(lat_str), float(lon_str)
        except Exception as e:
            logger.warning(f"Ошибка Яндекс Геокодера: {e}")

    simplified = re.sub(r"\b(г\.|ул\.|улица|д\.|дом)\s*", "", clean_addr, flags=re.IGNORECASE)
    simplified_no_str = re.sub(
        r",?\s*(стр\.?|строение|корп\.?|корпус)\s*\d+.*", "", simplified, flags=re.IGNORECASE
    )

    for candidate in [clean_addr, simplified, simplified_no_str]:
        try:
            async with httpx.AsyncClient(
                timeout=6.0,
                headers={"User-Agent": "GreydushyStudioBot/1.0 (https://greydushy.com)"},
            ) as client:
                resp = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": candidate, "format": "json", "limit": 1, "accept-language": "ru"},
                )
                if resp.status_code == 200 and resp.json():
                    item = resp.json()[0]
                    return float(item["lat"]), float(item["lon"])
        except Exception as e:
            logger.warning(f"Ошибка геокодирования '{candidate}': {e}")

    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Запускаем Telegram-бота и планировщик напоминаний из tg_bot.py
    polling_task, scheduler_task = await tg_bot.start_telegram_bot()

    yield

    if scheduler_task:
        scheduler_task.cancel()
    if polling_task:
        polling_task.cancel()
    bot = tg_bot.get_bot()
    if bot:
        await bot.session.close()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/webapp")
async def serve_mini_app():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/auth/check-admin/{platform}/{external_user_id}")
async def check_is_admin(
    platform: PlatformType, external_user_id: str, db: AsyncSession = Depends(get_db)
):
    admin_ids = [x.strip() for x in settings.ADMIN_TG_IDS.split(",") if x.strip()]
    if platform == PlatformType.TG and external_user_id in admin_ids:
        return {"is_admin": True}

    user = await crud.get_user_by_platform(db, platform, external_user_id)
    if user and user.role == "admin":
        return {"is_admin": True}

    return {"is_admin": False}


@app.post("/api/auth/admin-login")
async def login_admin_by_password(data: AdminLoginRequest):
    if data.password == settings.ADMIN_PASSWORD:
        return {"is_admin": True}
    raise HTTPException(status_code=401, detail="Неверный пароль кабинета")


@app.get("/api/geocode")
async def geocode_endpoint(address: str = Query(..., description="Адрес для поиска на карте")):
    coords = await resolve_address_coordinates(address)
    if not coords:
        raise HTTPException(status_code=404, detail="Координаты не найдены автоматически")
    return {"latitude": coords[0], "longitude": coords[1]}


@app.get("/api/users/me/{platform}/{external_user_id}", response_model=UserResponseSchema)
async def check_user_profile(
    platform: PlatformType, external_user_id: str, db: AsyncSession = Depends(get_db)
):
    user = await crud.get_user_by_platform(db, platform, external_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    return user


@app.post("/api/users/register", response_model=UserResponseSchema)
async def register_user(data: UserCreateSchema, db: AsyncSession = Depends(get_db)):
    return await crud.register_or_merge_user(db, data)


@app.post("/api/upload-cover")
async def upload_event_cover(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower() or ".jpg"
    filename = f"cover_{uuid.uuid4().hex[:10]}{ext}"
    file_path = UPLOADS_DIR / filename
    content = await file.read()
    file_path.write_bytes(content)
    return {"cover_url": f"/static/uploads/{filename}"}


@app.post("/api/events", response_model=EventResponseSchema)
async def create_event(data: EventCreateSchema, db: AsyncSession = Depends(get_db)):
    dump = data.model_dump()
    coords = await resolve_address_coordinates(dump["address"])
    if coords:
        dump["latitude"], dump["longitude"] = coords

    event = Event(**dump)
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return EventResponseSchema(
        **event.__dict__, occupied_seats=0, available_seats=event.max_seats
    )


@app.get("/api/events", response_model=list[EventResponseSchema])
async def list_events(db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Event)
        .where(Event.status == EventStatus.PUBLISHED)
        .order_by(Event.starts_at.asc())
    )
    events = (await db.execute(stmt)).scalars().all()
    response = []
    for ev in events:
        occupied = await crud.get_occupied_seats(db, ev.id)
        avail = max(0, ev.max_seats - occupied)
        response.append(
            EventResponseSchema(**ev.__dict__, occupied_seats=occupied, available_seats=avail)
        )
    return response


@app.put("/api/events/{event_id}", response_model=EventResponseSchema)
async def update_event(event_id: int, data: EventCreateSchema, db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Событие не найдено")

    dump = data.model_dump()
    if dump["address"] != event.address:
        coords = await resolve_address_coordinates(dump["address"])
        if coords:
            dump["latitude"], dump["longitude"] = coords

    for key, value in dump.items():
        setattr(event, key, value)
    await db.commit()
    await db.refresh(event)
    occupied = await crud.get_occupied_seats(db, event.id)
    avail = max(0, event.max_seats - occupied)
    return EventResponseSchema(**event.__dict__, occupied_seats=occupied, available_seats=avail)


@app.delete("/api/events/{event_id}")
async def delete_event(event_id: int, db: AsyncSession = Depends(get_db)):
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    await db.delete(event)
    await db.commit()
    return {"status": "deleted"}


@app.post("/api/registrations")
async def register_for_event(data: RegistrationCreateSchema, db: AsyncSession = Depends(get_db)):
    reg = await crud.book_event(db, data)
    email_ok = False
    user = await db.get(User, data.user_id)
    event = await db.get(Event, data.event_id)

    if reg.status == RegistrationStatus.REGISTERED:
        email_ok, _ = await send_confirmation_email(db, user, event, reg)

    # Мгновенно отправляем билет в личные сообщения Telegram-бота
    await tg_bot.notify_user_in_telegram(user, event, reg, db)

    return {
        "registration_id": reg.id,
        "status": reg.status,
        "email_sent": email_ok,
        "message": (
            "Вы записаны! Подтверждение отправлено в чат бота и на вашу почту."
            if reg.status == RegistrationStatus.REGISTERED
            else "Все места заняты, вы добавлены в лист ожидания."
        ),
    }


@app.get("/api/users/{user_id}/registrations")
async def get_user_registrations(user_id: int, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(Registration, Event)
        .join(Event, Registration.event_id == Event.id)
        .where(
            Registration.user_id == user_id,
            Registration.status != RegistrationStatus.CANCELLED,
        )
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "registration_id": reg.id,
            "event_id": ev.id,
            "event_title": ev.title,
            "starts_at": ev.starts_at,
            "address": ev.address,
            "status": reg.status,
        }
        for reg, ev in rows
    ]


@app.delete("/api/registrations/{reg_id}")
async def cancel_registration_api(reg_id: int, db: AsyncSession = Depends(get_db)):
    reg = await db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    was_active = reg.status in (RegistrationStatus.REGISTERED, RegistrationStatus.CONFIRMED)
    reg.status = RegistrationStatus.CANCELLED
    await db.commit()

    if was_active:
        wait_stmt = (
            select(Registration)
            .where(
                Registration.event_id == reg.event_id,
                Registration.status == RegistrationStatus.WAITLIST,
            )
            .order_by(Registration.created_at.asc())
            .limit(1)
        )
        next_in_line = (await db.execute(wait_stmt)).scalar_one_or_none()
        if next_in_line:
            next_in_line.status = RegistrationStatus.REGISTERED
            await db.commit()
            user = await db.get(User, next_in_line.user_id)
            event = await db.get(Event, next_in_line.event_id)
            await send_confirmation_email(db, user, event, next_in_line)
            await tg_bot.notify_user_in_telegram(user, event, next_in_line, db)

    return {"status": "cancelled"}


@app.patch("/api/admin/registrations/{reg_id}/status/{new_status}")
async def update_guest_status(
    reg_id: int, new_status: RegistrationStatus, db: AsyncSession = Depends(get_db)
):
    reg = await db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    reg.status = new_status
    await db.commit()

    if new_status == RegistrationStatus.REGISTERED and not reg.email_sent:
        user = await db.get(User, reg.user_id)
        event = await db.get(Event, reg.event_id)
        await send_confirmation_email(db, user, event, reg)
        await tg_bot.notify_user_in_telegram(user, event, reg, db)

    return {"registration_id": reg.id, "status": reg.status, "email_sent": reg.email_sent}


@app.post("/api/admin/registrations/{reg_id}/resend-email")
async def resend_guest_confirmation_email(reg_id: int, db: AsyncSession = Depends(get_db)):
    reg = await db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    user = await db.get(User, reg.user_id)
    event = await db.get(Event, reg.event_id)
    ok, msg = await send_confirmation_email(db, user, event, reg)
    if not ok:
        raise HTTPException(status_code=502, detail=msg)
    return {"email_sent": True, "message": f"Письмо повторно отправлено на {user.email}"}


@app.post("/api/admin/events/{event_id}/broadcast")
async def broadcast_event_from_admin(event_id: int, db: AsyncSession = Depends(get_db)):
    """Рассылает анонс выбранного мастер-класса всем подписчикам Telegram-бота"""
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    sent_count = await tg_bot.broadcast_event_announcement(event, db)
    return {"sent_count": sent_count, "message": f"Анонс отправлен {sent_count} подписчикам бота!"}


@app.get("/api/admin/events/{event_id}/tg-link")
async def get_event_tg_deep_link(event_id: int):
    """Возвращает прямую ссылку на запись в боте для вставки в пост Telegram-канала"""
    username = tg_bot.bot_username
    if not username:
        bot = tg_bot.get_bot()
        if bot:
            me = await bot.get_me()
            username = me.username or ""
            tg_bot.bot_username = username
    if not username:
        raise HTTPException(status_code=400, detail="Бот еще не подключен или токен не задан")
    return {"deep_link": f"https://t.me/{username}?start=event_{event_id}"}


@app.get("/api/admin/registrations")
async def get_all_registrations_for_admin(
    event_id: int | None = None, db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(Registration, User, Event)
        .join(User, Registration.user_id == User.id)
        .join(Event, Registration.event_id == Event.id)
        .order_by(Registration.created_at.desc())
    )
    if event_id:
        stmt = stmt.where(Registration.event_id == event_id)

    rows = (await db.execute(stmt)).all()
    return [
        {
            "registration_id": reg.id,
            "event_id": ev.id,
            "event_title": ev.title,
            "last_name": u.last_name,
            "first_name": u.first_name,
            "patronymic": u.patronymic,
            "phone": u.phone,
            "email": u.email,
            "vk_profile": u.vk_profile,
            "status": reg.status,
            "email_sent": reg.email_sent,
            "created_at": reg.created_at,
        }
        for reg, u, ev in rows
    ]
