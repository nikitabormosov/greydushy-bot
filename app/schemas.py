from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field
from app.models import PlatformType, EventStatus, RegistrationStatus


class UserCreateSchema(BaseModel):
    last_name: str = Field(..., min_length=1, description="Фамилия")
    first_name: str = Field(..., min_length=1, description="Имя")
    patronymic: Optional[str] = Field(None, description="Отчество (при наличии)")
    phone: str = Field(..., min_length=10, description="Телефон")
    email: EmailStr = Field(..., description="Почта")
    vk_profile: str = Field(..., min_length=2, description="Ссылка или никнейм в ВК")
    pdn_accepted: bool = Field(..., description="Согласие на обработку ПДн")
    platform: PlatformType
    external_user_id: str
    username: Optional[str] = None


class UserResponseSchema(BaseModel):
    id: int
    last_name: str
    first_name: str
    patronymic: Optional[str]
    phone: str
    email: str
    vk_profile: str
    role: str

    class Config:
        from_attributes = True


class EventCreateSchema(BaseModel):
    title: str = Field(..., example="Мастер-класс «Тюмень в лоскутках»")
    kind: str = Field(..., min_length=1, example="Социальный")
    excerpt: Optional[str] = Field(None, example="Узнай историю и легенды Тюмени через рукоделие!")
    description: str = Field(..., example="Подробный рассказ о встрече...")
    cover_url: Optional[str] = None
    price: Optional[str] = Field("Бесплатно", example="Бесплатно")
    duration: Optional[str] = Field("3 часа", example="3 часа")
    starts_at: datetime
    max_seats: int = Field(12, gt=0)
    address: str = Field(..., example="г. Тюмень, ул. Ванцетти, 1")
    location_details: Optional[str] = Field(None, example="1 этаж")
    latitude: Optional[float] = Field(None, example=57.1553)
    longitude: Optional[float] = Field(None, example=65.5619)
    requires_safety_ack: bool = True


class EventResponseSchema(EventCreateSchema):
    id: int
    status: EventStatus
    occupied_seats: int = 0
    available_seats: int = 0

    class Config:
        from_attributes = True


class RegistrationCreateSchema(BaseModel):
    user_id: int
    event_id: int
    safety_accepted: bool = Field(..., description="Согласие с техникой безопасности")
