import logging
from datetime import datetime
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import User, Event, Registration

logger = logging.getLogger(__name__)

MONTHS_RU = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def format_event_datetime(dt: datetime) -> tuple[str, str]:
    """
    Превращает datetime(2026, 10, 18, 16, 0)
    в ("18 октября", "16:00") специально под формат api.greydushy.com/webhook
    """
    event_date = f"{dt.day} {MONTHS_RU[dt.month]}"
    event_time = dt.strftime("%H:%M")
    return event_date, event_time


async def send_confirmation_email(
    db: AsyncSession, user: User, event: Event, registration: Registration
) -> tuple[bool, str]:
    """
    Отправляет POST JSON-запрос на https://api.greydushy.com/webhook:
    {
      "email": "...",
      "name": "...",
      "event_title": "...",
      "event_date": "18 октября",
      "event_time": "16:00",
      "event_place": "г. Тюмень, ..."
    }
    """
    webhook_url = settings.EMAIL_API_URL or "https://api.greydushy.com/webhook"
    event_date, event_time = format_event_datetime(event.starts_at)

    event_place = event.address
    if event.location_details:
        event_place = f"{event.address} ({event.location_details})"

    payload = {
        "email": user.email,
        "name": user.first_name,
        "event_title": event.title,
        "event_date": event_date,
        "event_time": event_time,
        "event_place": event_place,
    }

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            response = await client.post(
                webhook_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()

        registration.email_sent = True
        await db.commit()
        logger.info(f"✅ Вебхук письма успешно отправлен на {user.email}: {payload}")
        return True, "Письмо успешно отправлено"

    except httpx.HTTPStatusError as e:
        err_msg = f"Сервер почты вернул код {e.response.status_code}: {e.response.text}"
        logger.error(err_msg)
        return False, err_msg
    except Exception as e:
        err_msg = f"Ошибка соединения с {webhook_url}: {e}"
        logger.error(err_msg)
        return False, err_msg
