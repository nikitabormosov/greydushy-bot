import re
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from app.models import User, UserPlatform, Event, Registration, RegistrationStatus, PlatformType
from app.schemas import UserCreateSchema, EventCreateSchema, RegistrationCreateSchema


def normalize_phone(phone: str) -> str:
    """Приводит номер телефона к единому формату +7XXXXXXXXXX для склейки профилей"""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return f"+{digits}"


async def get_user_by_platform(db: AsyncSession, platform: PlatformType, external_id: str) -> User | None:
    stmt = (
        select(User)
        .join(UserPlatform)
        .where(UserPlatform.platform == platform, UserPlatform.external_user_id == external_id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def register_or_merge_user(db: AsyncSession, data: UserCreateSchema) -> User:
    if not data.pdn_accepted:
        raise HTTPException(status_code=400, detail="Необходимо согласие на обработку персональных данных")

    clean_phone = normalize_phone(data.phone)

    # 1. Проверяем, нет ли уже гостя с таким телефоном (например, заходил из другого мессенджера)
    stmt = select(User).where(User.phone == clean_phone)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        # Обновляем анкетные данные при необходимости
        user.last_name = data.last_name
        user.first_name = data.first_name
        user.patronymic = data.patronymic
        user.email = data.email
        user.vk_profile = data.vk_profile
    else:
        # Создаем нового гостя
        user = User(
            last_name=data.last_name,
            first_name=data.first_name,
            patronymic=data.patronymic,
            phone=clean_phone,
            email=data.email,
            vk_profile=data.vk_profile,
            pdn_accepted_at=datetime.utcnow(),
        )
        db.add(user)
        await db.flush()

    # 2. Привязываем платформу (TG / VK / MAX), если еще не привязана
    plat_stmt = select(UserPlatform).where(
        UserPlatform.platform == data.platform,
        UserPlatform.external_user_id == data.external_user_id,
    )
    plat_res = await db.execute(plat_stmt)
    if not plat_res.scalar_one_or_none():
        db.add(
            UserPlatform(
                user_id=user.id,
                platform=data.platform,
                external_user_id=data.external_user_id,
                username=data.username,
            )
        )

    await db.commit()
    await db.refresh(user)
    return user


async def get_occupied_seats(db: AsyncSession, event_id: int) -> int:
    """Считает активные занятые места (Записан + Подтвержден + Предложено из очереди)"""
    active_statuses = [
        RegistrationStatus.REGISTERED,
        RegistrationStatus.CONFIRMED,
        RegistrationStatus.OFFERED,
    ]
    stmt = select(func.count(Registration.id)).where(
        Registration.event_id == event_id,
        Registration.status.in_(active_statuses),
    )
    result = await db.execute(stmt)
    return result.scalar() or 0


async def book_event(db: AsyncSession, data: RegistrationCreateSchema) -> Registration:
    event = await db.get(Event, data.event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Мероприятие не найдено")

    if event.requires_safety_ack and not data.safety_accepted:
        raise HTTPException(
            status_code=400,
            detail="Необходимо подтвердить ознакомление с правилами техники безопасности",
        )

    # Проверяем, не записан ли уже гость
    existing_stmt = select(Registration).where(
        Registration.user_id == data.user_id,
        Registration.event_id == data.event_id,
    )
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing and existing.status != RegistrationStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Вы уже записаны на это событие")

    occupied = await get_occupied_seats(db, event.id)
    new_status = (
        RegistrationStatus.REGISTERED
        if occupied < event.max_seats
        else RegistrationStatus.WAITLIST
    )

    if existing:
        existing.status = new_status
        existing.safety_accepted_at = datetime.utcnow()
        reg = existing
    else:
        reg = Registration(
            user_id=data.user_id,
            event_id=data.event_id,
            status=new_status,
            safety_accepted_at=datetime.utcnow(),
        )
        db.add(reg)

    await db.commit()
    await db.refresh(reg)
    return reg
