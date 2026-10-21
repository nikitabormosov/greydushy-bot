import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Event, EventStatus, PlatformType, Registration, RegistrationStatus
from app.schemas import UserCreateSchema, RegistrationCreateSchema
from app import crud
from app.email_service import send_confirmation_email, format_event_datetime

logger = logging.getLogger(__name__)

router = Router()


class OnboardingFSM(StatesGroup):
    """Пошаговая анкета в чате (резервный вариант до подключения Mini App)"""
    waiting_for_fio = State()
    waiting_for_phone = State()
    waiting_for_email = State()
    waiting_for_vk = State()
    waiting_for_pdn = State()


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Афиша мастер-классов", callback_data="menu_events")],
            [InlineKeyboardButton(text="📋 Мои записи", callback_data="menu_my_regs")],
        ]
    )


async def render_event_card(event_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует текст карточки мастер-класса и инлайн-кнопки"""
    async with AsyncSessionLocal() as db:
        event = await db.get(Event, event_id)
        if not event or event.status != EventStatus.PUBLISHED:
            return "Мероприятие не найдено или запись уже закрыта.", main_menu_kb()

        occupied = await crud.get_occupied_seats(db, event.id)
        avail = max(0, event.max_seats - occupied)
        ev_date, ev_time = format_event_datetime(event.starts_at)

        place_str = event.address
        if event.location_details:
            place_str += f" ({event.location_details})"

        safety_note = ""
        if event.requires_safety_ack:
            safety_note = (
                f"\n\n⚠️ <b>Техника безопасности:</b> на мастер-классе используются колющие и режущие "
                f"инструменты. Нажимая кнопку записи, вы подтверждаете согласие с "
                f"<a href='{settings.SAFETY_PRECAUTIONS_URL}'>Правилами техники безопасности</a>."
            )

        text = (
            f"✨ <b>{event.title}</b>\n\n"
            f"{event.description}\n\n"
            f"📅 <b>Дата:</b> {ev_date}\n"
            f"⏰ <b>Время:</b> {ev_time}\n"
            f"📍 <b>Место:</b> {place_str}\n"
            f"🎟 <b>Свободных мест:</b> {avail} из {event.max_seats}"
            f"{safety_note}"
        )

        btn_text = (
            "✅ Записаться (с ТБ ознакомлен)"
            if avail > 0
            else "⏳ Встать в лист ожидания (с ТБ ознакомлен)"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=btn_text, callback_data=f"book_{event.id}")],
                [InlineKeyboardButton(text="⬅️ К списку событий", callback_data="menu_events")],
            ]
        )
        return text, kb


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    args = command.args

    # Если человек перешел по deep-link ссылке вида t.me/bot?start=event_1
    if args and args.startswith("event_"):
        try:
            event_id = int(args.split("_")[1])
            text, kb = await render_event_card(event_id)
            await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            return
        except ValueError:
            pass

    await message.answer(
        "Добро пожаловать в студию дизайна <b>«Грея душу»</b>! 🌿\n\n"
        "Здесь можно за пару секунд записаться на наши мастер-классы, презентации и творческие встречи.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu_events")
async def show_events_list(call: CallbackQuery):
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Event)
            .where(Event.status == EventStatus.PUBLISHED)
            .order_by(Event.starts_at.asc())
        )
        events = (await db.execute(stmt)).scalars().all()

        if not events:
            await call.message.edit_text(
                "Пока нет открытых мастер-классов. Следите за нашими анонсами!",
                reply_markup=main_menu_kb(),
            )
            return

        buttons = []
        for ev in events:
            ev_date, ev_time = format_event_datetime(ev.starts_at)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"📌 {ev.title} ({ev_date} в {ev_time})",
                        callback_data=f"event_{ev.id}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_home")])

        await call.message.edit_text(
            "Выберите мероприятие, чтобы узнать подробности и записаться:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data == "back_home")
async def back_home_handler(call: CallbackQuery):
    await call.message.edit_text(
        "Главное меню студии <b>«Грея душу»</b>:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("event_"))
async def open_event_handler(call: CallbackQuery):
    event_id = int(call.data.split("_")[1])
    text, kb = await render_event_card(event_id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("book_"))
async def book_event_handler(call: CallbackQuery, state: FSMContext):
    event_id = int(call.data.split("_")[1])
    tg_id = str(call.from_user.id)

    async with AsyncSessionLocal() as db:
        user = await crud.get_user_by_platform(db, PlatformType.TG, tg_id)

        # Если гость еще ни разу не заполнял анкету — запускаем быстрое знакомство
        if not user:
            await state.update_data(target_event_id=event_id)
            await state.set_state(OnboardingFSM.waiting_for_fio)
            await call.message.answer(
                "Для первой записи давай познакомимся (в следующий раз бот запомнит тебя, и запись будет в 1 клик!).\n\n"
                "Напиши свои <b>Фамилию, Имя и Отчество</b> (отчество — при наличии) через пробел:",
                parse_mode="HTML",
            )
            await call.answer()
            return

        # Если профиль уже есть — записываем мгновенно!
        try:
            reg = await crud.book_event(
                db,
                RegistrationCreateSchema(user_id=user.id, event_id=event_id, safety_accepted=True),
            )
            event = await db.get(Event, event_id)
            ev_date, ev_time = format_event_datetime(event.starts_at)

            if reg.status == RegistrationStatus.REGISTERED:
                # Отправляем JSON-вебхук на api.greydushy.com/webhook
                await send_confirmation_email(db, user, event, reg)
                msg = (
                    f"🎉 <b>{user.first_name}, вы успешно записаны!</b>\n\n"
                    f"📌 <b>Событие:</b> {event.title}\n"
                    f"📅 <b>Когда:</b> {ev_date} в {ev_time}\n"
                    f"📍 <b>Адрес:</b> {event.address}\n\n"
                    f"✉️ Письмо с подтверждением также отправлено на вашу почту <b>{user.email}</b>."
                )
            else:
                msg = (
                    f"⏳ <b>Все основные места уже заняты, но мы добавили вас в Лист ожидания!</b>\n\n"
                    f"Как только кто-то отменит запись на «{event.title}», бот сразу напишет вам."
                )

            await call.message.edit_text(msg, reply_markup=main_menu_kb(), parse_mode="HTML")
        except Exception as e:
            detail = getattr(e, "detail", str(e))
            await call.answer(f"⚠️ {detail}", show_alert=True)


# --- Пошаговое заполнение анкеты для новых гостей ---

@router.message(OnboardingFSM.waiting_for_fio)
async def process_fio(message: Message, state: FSMContext):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Пожалуйста, укажи как минимум Фамилию и Имя через пробел:")
        return

    last_name = parts[0]
    first_name = parts[1]
    patronymic = " ".join(parts[2:]) if len(parts) > 2 else None

    await state.update_data(last_name=last_name, first_name=first_name, patronymic=patronymic)
    await state.set_state(OnboardingFSM.waiting_for_phone)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Отлично! Теперь нажми кнопку ниже, чтобы поделиться телефоном, или напиши его вручную:",
        reply_markup=kb,
    )


@router.message(OnboardingFSM.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    await state.update_data(phone=phone)
    await state.set_state(OnboardingFSM.waiting_for_email)
    await message.answer(
        "Напиши свою <b>Почту (Email)</b> — на нее придет письмо с подтверждением записи:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )


@router.message(OnboardingFSM.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if "@" not in email or "." not in email:
        await message.answer("Кажется, в адресе почты опечатка. Попробуй еще раз (например, name@mail.ru):")
        return

    await state.update_data(email=email)
    await state.set_state(OnboardingFSM.waiting_for_vk)
    await message.answer("Укажи ссылку (или никнейм) на твой <b>Профиль ВК</b>:", parse_mode="HTML")


@router.message(OnboardingFSM.waiting_for_vk)
async def process_vk(message: Message, state: FSMContext):
    await state.update_data(vk_profile=message.text.strip())
    await state.set_state(OnboardingFSM.waiting_for_pdn)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Ознакомлен(а) и согласен(на)", callback_data="pdn_accept")]
        ]
    )
    await message.answer(
        "Последний шаг — юридическое согласие:\n\n"
        f"• <a href='{settings.PRIVACY_POLICY_URL}'>Политика по обработке персональных данных</a>\n"
        f"• <a href='{settings.PRIVACY_AGREEMENT_URL}'>Согласие на обработку персональных данных</a>\n\n"
        "Нажми кнопку ниже, чтобы подтвердить согласие и завершить запись:",
        reply_markup=kb,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(OnboardingFSM.waiting_for_pdn, F.data == "pdn_accept")
async def process_pdn_and_finish(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    async with AsyncSessionLocal() as db:
        user = await crud.register_or_merge_user(
            db,
            UserCreateSchema(
                last_name=data["last_name"],
                first_name=data["first_name"],
                patronymic=data.get("patronymic"),
                phone=data["phone"],
                email=data["email"],
                vk_profile=data["vk_profile"],
                pdn_accepted=True,
                platform=PlatformType.TG,
                external_user_id=str(call.from_user.id),
                username=call.from_user.username,
            ),
        )

        event_id = data.get("target_event_id")
        if event_id:
            reg = await crud.book_event(
                db,
                RegistrationCreateSchema(user_id=user.id, event_id=event_id, safety_accepted=True),
            )
            event = await db.get(Event, event_id)
            ev_date, ev_time = format_event_datetime(event.starts_at)

            if reg.status == RegistrationStatus.REGISTERED:
                await send_confirmation_email(db, user, event, reg)
                await call.message.edit_text(
                    f"🎉 <b>Анкета сохранена, и вы записаны на «{event.title}»!</b>\n\n"
                    f"📅 <b>Дата:</b> {ev_date} в {ev_time}\n"
                    f"📍 <b>Место:</b> {event.address}\n\n"
                    f"✉️ Подтверждение отправлено на почту <b>{user.email}</b>.",
                    reply_markup=main_menu_kb(),
                    parse_mode="HTML",
                )
            else:
                await call.message.edit_text(
                    f"Анкета сохранена! Вы добавлены в лист ожидания на «{event.title}».",
                    reply_markup=main_menu_kb(),
                )


@router.callback_query(F.data == "menu_my_regs")
async def show_my_registrations(call: CallbackQuery):
    async with AsyncSessionLocal() as db:
        user = await crud.get_user_by_platform(db, PlatformType.TG, str(call.from_user.id))
        if not user:
            await call.answer("У вас пока нет активных записей.", show_alert=True)
            return

        stmt = (
            select(Registration, Event)
            .join(Event, Registration.event_id == Event.id)
            .where(
                Registration.user_id == user.id,
                Registration.status != RegistrationStatus.CANCELLED,
            )
        )
        rows = (await db.execute(stmt)).all()
        if not rows:
            await call.message.edit_text("У вас пока нет активных записей.", reply_markup=main_menu_kb())
            return

        lines = ["📋 <b>Ваши активные записи:</b>\n"]
        buttons = []
        for reg, ev in rows:
            ev_date, ev_time = format_event_datetime(ev.starts_at)
            status_label = "✅ Записан" if reg.status == RegistrationStatus.REGISTERED else "⏳ В очереди"
            lines.append(f"• <b>{ev.title}</b> — {ev_date} в {ev_time} ({status_label})\n📍 {ev.address}\n")
            buttons.append(
                [InlineKeyboardButton(text=f"❌ Отменить: {ev.title}", callback_data=f"cancel_{reg.id}")]
            )

        buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_home")])
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("cancel_"))
async def cancel_registration_handler(call: CallbackQuery):
    reg_id = int(call.data.split("_")[1])
    async with AsyncSessionLocal() as db:
        reg = await db.get(Registration, reg_id)
        if reg:
            reg.status = RegistrationStatus.CANCELLED
            await db.commit()
    await call.answer("Запись отменена", show_alert=True)
    await show_my_registrations(call)
