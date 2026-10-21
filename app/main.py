import asyncio
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot, Dispatcher

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
from app.tg_bot import router as tg_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "static" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


async def resolve_address_coordinates(address: str) -> tuple[float, float] | None:
    """
    Находит точные координаты (широту и долготу) по текстовому адресу.
    Поддерживает Яндекс Геокодер (если задан ключ) и умный поиск по Тюмени.
    """
    clean_addr = address.strip()
    if "тюмень" not in clean_addr.lower():
        clean_addr = f"г. Тюмень, {clean_addr}"

    # 1. Если в .env задан ключ Яндекс Карт — используем официальный геокодер Яндекса
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
                        pos = members[0]["GeoObject"]["Point"]["pos"]  # "lon lat"
                        lon_str, lat_str = pos.split()
                        return float(lat_str), float(lon_str)
        except Exception as e:
            logger.warning(f"Ошибка Яндекс Геокодера: {e}")

    # 2. Универсальный поиск с очисткой сложных сокращений ("ул. 25-го Октября, 23А, стр.1")
    simplified = re.sub(r"\b(г\.|ул\.|улица|д\.|дом)\s*", "", clean_addr, flags=re.IGNORECASE)
    simplified_no_str = re.sub(r",?\s*(стр\.?|строение|корп\.?|корпус)\s*\d+.*", "", simplified, flags=re.IGNORECASE)

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

    bot_task = None
    bot = None
    if settings.TG_BOT_TOKEN and settings.TG_BOT_TOKEN != "your_telegram_bot_token":
        bot = Bot(token=settings.TG_BOT_TOKEN)
        dp = Dispatcher()
        dp.include_router(tg_router)
        bot_task = asyncio.create_task(dp.start_polling(bot))
        logger.info("Telegram-бот успешно запущен!")

    yield

    if bot_task:
        bot_task.cancel()
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


from pydantic import BaseModel


class AdminLoginRequest(BaseModel):
    password: str


@app.get("/api/auth/check-admin/{platform}/{external_user_id}")
async def check_is_admin(
    platform: PlatformType, external_user_id: str, db: AsyncSession = Depends(get_db)
):
    """Проверяет, является ли пользователь администратором по Telegram ID или роли в БД"""
    admin_ids = [x.strip() for x in settings.ADMIN_TG_IDS.split(",") if x.strip()]
    if platform == PlatformType.TG and external_user_id in admin_ids:
        return {"is_admin": True}

    user = await crud.get_user_by_platform(db, platform, external_user_id)
    if user and user.role == "admin":
        return {"is_admin": True}

    return {"is_admin": False}


@app.post("/api/auth/admin-login")
async def login_admin_by_password(data: AdminLoginRequest):
    """Вход в Кабинет администратора из браузера по паролю"""
    if data.password == settings.ADMIN_PASSWORD:
        return {"is_admin": True}
    raise HTTPException(status_code=401, detail="Неверный пароль кабинета")


@app.get("/api/geocode")
async def geocode_endpoint(address: str = Query(..., description="Адрес для поиска на карте")):
    """Серверный поиск координат по адресу в Тюмени"""
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
    # Если координаты не переданы или остались дефолтными — геокодируем адрес автоматически
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
    """Запись на мастер-класс с мгновенной отправкой письма через api.greydushy.com/webhook"""
    reg = await crud.book_event(db, data)
    email_ok = False

    if reg.status == RegistrationStatus.REGISTERED:
        user = await db.get(User, data.user_id)
        event = await db.get(Event, data.event_id)
        email_ok, _ = await send_confirmation_email(db, user, event, reg)

    return {
        "registration_id": reg.id,
        "status": reg.status,
        "email_sent": email_ok,
        "message": (
            f"Вы записаны! Письмо с деталями отправлено на вашу почту."
            if reg.status == RegistrationStatus.REGISTERED and email_ok
            else (
                "Вы успешно записаны на встречу!"
                if reg.status == RegistrationStatus.REGISTERED
                else "Все места заняты, вы добавлены в лист ожидания."
            )
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
    """Отмена записи гостем + автоперевод первого человека из Листа ожидания с отправкой ему письма"""
    reg = await db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    was_active = reg.status in (RegistrationStatus.REGISTERED, RegistrationStatus.CONFIRMED)
    reg.status = RegistrationStatus.CANCELLED
    await db.commit()

    # Если место освободилось — берем первого из листа ожидания и отправляем ему письмо!
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

    return {"status": "cancelled"}


@app.patch("/api/admin/registrations/{reg_id}/status/{new_status}")
async def update_guest_status(
    reg_id: int, new_status: RegistrationStatus, db: AsyncSession = Depends(get_db)
):
    """Изменение статуса гостя администратором (если перевели в состав и письмо еще не уходило — отправляем)"""
    reg = await db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    reg.status = new_status
    await db.commit()

    if new_status == RegistrationStatus.REGISTERED and not reg.email_sent:
        user = await db.get(User, reg.user_id)
        event = await db.get(Event, reg.event_id)
        await send_confirmation_email(db, user, event, reg)

    return {"registration_id": reg.id, "status": reg.status, "email_sent": reg.email_sent}


@app.post("/api/admin/registrations/{reg_id}/resend-email")
async def resend_guest_confirmation_email(reg_id: int, db: AsyncSession = Depends(get_db)):
    """Ручная повторная отправка письма подтверждения из Кабинета администратора"""
    reg = await db.get(Registration, reg_id)
    if not reg:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    user = await db.get(User, reg.user_id)
    event = await db.get(Event, reg.event_id)
    ok, msg = await send_confirmation_email(db, user, event, reg)
    if not ok:
        raise HTTPException(status_code=502, detail=msg)
    return {"email_sent": True, "message": f"Письмо повторно отправлено на {user.email}"}


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
