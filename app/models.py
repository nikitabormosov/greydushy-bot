import enum
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Integer, Float, Boolean, DateTime, ForeignKey, Enum, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class PlatformType(str, enum.Enum):
    TG = "tg"
    VK = "vk"
    MAX = "max"


class UserRole(str, enum.Enum):
    GUEST = "guest"
    ADMIN = "admin"


class EventStatus(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    FINISHED = "finished"


class RegistrationStatus(str, enum.Enum):
    REGISTERED = "registered"    # Записан в основной состав
    CONFIRMED = "confirmed"      # Подтвердил явку за 24 часа
    WAITLIST = "waitlist"        # В листе ожидания
    OFFERED = "offered"          # Предложено освободившееся место (таймер 2ч)
    CANCELLED = "cancelled"      # Отменил запись
    ATTENDED = "attended"        # Пришел на мероприятие (чек-ин)


class User(Base):
    """Единая анкета гостя студии (все поля из исходной Яндекс Формы)"""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    patronymic: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # При наличии
    phone: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(150), nullable=False)
    vk_profile: Mapped[str] = mapped_column(String(255), nullable=False)

    # Юридическое согласие с Политикой обработки персональных данных
    pdn_accepted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.GUEST)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    platforms: Mapped[List["UserPlatform"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    registrations: Mapped[List["Registration"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserPlatform(Base):
    """Привязка аккаунтов Telegram, ВК и MAX к единому профилю гостя"""
    __tablename__ = "user_platforms"
    __table_args__ = (UniqueConstraint("platform", "external_user_id", name="uq_platform_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    platform: Mapped[PlatformType] = mapped_column(Enum(PlatformType), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    is_subscribed: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped["User"] = relationship(back_populates="platforms")


class Event(Base):
    """Мастер-классы и события студии"""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(100), nullable=False, default="Творческий")  # Заполняется админом вручную
    excerpt: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # Краткое описание для карточки в афише
    description: Mapped[str] = mapped_column(Text, nullable=False)  # Полное описание на странице события
    cover_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    price: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default="Бесплатно")
    duration: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, default="2 часа")

    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    max_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=12)

    address: Mapped[str] = mapped_column(String(300), nullable=False)
    location_details: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    requires_safety_ack: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[EventStatus] = mapped_column(Enum(EventStatus), default=EventStatus.PUBLISHED)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    registrations: Mapped[List["Registration"]] = relationship(back_populates="event", cascade="all, delete-orphan")


class Registration(Base):
    """Записи гостей на события и Лист ожидания"""
    __tablename__ = "registrations"
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_user_event"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    status: Mapped[RegistrationStatus] = mapped_column(Enum(RegistrationStatus), nullable=False)

    # Подтверждение правил техники безопасности для конкретного мастер-класса
    safety_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    waitlist_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="registrations")
    event: Mapped["Event"] = relationship(back_populates="registrations")
