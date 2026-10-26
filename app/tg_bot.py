import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    User,
    UserPlatform,
    Event,
    EventStatus,
    PlatformType,
    Registration,
    RegistrationStatus,
)
from app.schemas import UserCreateSchema, RegistrationCreateSchema
from app import crud
from app.email_service import send_confirmation_email, format_event_datetime

logger = logging.getLogger(__name__)

router = Router()
dp = Dispatcher()
dp.include_router(router)

# Глобальный объект бота (создается при старте с ключом TG_BOT_TOKEN из .env)
bot_instance: Bot | None = None

# Память отправленных напоминаний, чтобы не слать дубли
sent_24h_reminders: set[int] = set()
sent_2h_reminders: set[int] = set()


class OnboardingFSM(StatesGroup):
    waiting_for_fio = State()
    waiting_for_phone = State()
    waiting_for_email = State()
    waiting_for_vk = State()
    waiting_for_pdn = State()


def get_bot() -> Bot | None:
    """Создает или возвращает объект бота с токеном из настроек"""
    global bot_instance
    if bot_instance is None:
        token = (settings.TG_BOT_TOKEN or "").strip()
        if token and token != "your_telegram_bot_token":
            session = None
            cf_url = getattr(settings, "CF_WORKER_URL", None)

            if cf_url:
                # В aiogram метод называется from_base, а параметр в сессии — api
                api_server = TelegramAPIServer.from_base(cf_url.strip())
                session = AiohttpSession(api=api_server)

            bot_instance = Bot(token=token, session=session)
    return bot_instance


async def start_telegram_bot() -> tuple[asyncio.Task | None, asyncio.Task | None]:
    """
    Запускает Telegram-бота (Long Polling) и фоновый планировщик напоминаний (24ч / 2ч).
    Вызывается автоматически при старте FastAPI в main.py.
    """
    bot = get_bot()
    if not bot:
        logger.warning("⚠️ TG_BOT_TOKEN не задан в .env — Telegram-бот не запущен!")
        return None, None

    try:
        # Обязательно удаляем старый webhook в Telegram, иначе /start не дойдет до polling!
        await bot.delete_webhook(drop_pending_updates=True)
        await setup_bot_menu_button(bot)
        me = await bot.get_me()
        logger.info(f"✅ Telegram-бот @{me.username} (ID: {me.id}) успешно подключен к API!")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Telegram API (проверь TG_BOT_TOKEN): {e}")
        return None, None

    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    scheduler_task = asyncio.create_task(reminder_scheduler_loop())
    return polling_task, scheduler_task


def main_menu_kb(event_id: int | None = None) -> InlineKeyboardMarkup:
    """Клавиатура под приветственным сообщением"""
    buttons = []
    if settings.WEBAPP_URL and settings.WEBAPP_URL.startswith("https://"):
        url = settings.WEBAPP_URL
        if event_id:
            url = f"{url}?event_id={event_id}"
        buttons.append(
            [InlineKeyboardButton(text="✨ Открыть Афишу", web_app=WebAppInfo(url=url))]
        )

    buttons.append([InlineKeyboardButton(text="🎨 Список встреч в чате", callback_data="menu_events")])
    buttons.append([InlineKeyboardButton(text="📋 Мои записи", callback_data="menu_my_regs")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def setup_bot_menu_button(bot: Bot):
    """Устанавливает кнопку 'Афиша' слева от поля ввода сообщения"""
    if settings.WEBAPP_URL and settings.WEBAPP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Афиша",
                    web_app=WebAppInfo(url=settings.WEBAPP_URL),
                )
            )
        except Exception as e:
            logger.warning(f"Не удалось установить MenuButtonWebApp: {e}")


async def save_telegram_subscriber(tg_user_id: str, username: str | None = None):
    """Сохраняет или обновляет контакт в UserPlatform при нажатии /start"""
    async with AsyncSessionLocal() as db:
        stmt = select(UserPlatform).where(
            UserPlatform.platform == PlatformType.TG,
            UserPlatform.external_user_id == tg_user_id,
        )
        plat = (await db.execute(stmt)).scalar_one_or_none()
        if plat:
            plat.is_subscribed = True
            if username:
                plat.username = username
            await db.commit()


async def notify_user_in_telegram(user: User, event: Event, reg: Registration, db):
    """Отправляет красивое сообщение-билет в чат Telegram при записи через Mini App"""
    bot = get_bot()
    if not bot:
        return

    stmt = select(UserPlatform).where(
        UserPlatform.user_id == user.id,
        UserPlatform.platform == PlatformType.TG,
        ~UserPlatform.external_user_id.like("web_%"),
    )
    plat = (await db.execute(stmt)).scalars().first()
    if not plat:
        return

    ev_date, ev_time = format_event_datetime(event.starts_at)
    place = f"{event.address} ({event.location_details})" if event.location_details else event.address

    if reg.status == RegistrationStatus.REGISTERED:
        text = (
            f"🧡 <b>{user.first_name}, место за вами!</b>\n\n"
            f"Мы ждём вас на мероприятии:\n"
            f"📌 <b>{event.title}</b> ({event.kind})\n"
            f"📅 <b>Когда:</b> {ev_date} в {ev_time}\n"
            f"📍 <b>Где:</b> {place}\n"
            f"🎟 <b>Стоимость:</b> {event.price or 'Бесплатно'}\n\n"
            f"✉️ Письмо с подтверждением также отправлено на вашу почту <b>{user.email}</b>.\n"
            f"За сутки и за 2 часа до начала мы пришлём сюда напоминание!"
        )
    else:
        text = (
            f"⏳ <b>Вы добавлены в лист ожидания!</b>\n\n"
            f"На встречу <b>«{event.title}»</b> ({ev_date} в {ev_time}) сейчас заняты все места, "
            f"но как только появится свободное окошко — мы сразу напишем вам сюда."
        )

    try:
        await bot.send_message(
            chat_id=int(plat.external_user_id),
            text=text,
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить билет в TG пользователю {plat.external_user_id}: {e}")


async def broadcast_event_announcement(event: Event, db) -> int:
    """Рассылает анонс выбранного мастер-класса всем пользователям бота"""
    bot = get_bot()
    if not bot:
        return 0

    stmt = select(UserPlatform).where(
        UserPlatform.platform == PlatformType.TG,
        UserPlatform.is_subscribed == True,
    )
    platforms = (await db.execute(stmt)).scalars().all()

    ev_date, ev_time = format_event_datetime(event.starts_at)
    short_desc = event.excerpt or event.description

    text = (
        f"✨ <b>Новое мероприятие!</b>\n\n"
        f"🏷 <i>{event.kind}</i>\n"
        f"<b>{event.title}</b>\n\n"
        f"{short_desc}\n\n"
        f"📅 <b>Когда:</b> {ev_date} в {ev_time} ({event.duration or '2 часа'})\n"
        f"📍 <b>Где:</b> {event.address}\n"
        f"🎟 <b>Участие:</b> {event.price or 'Бесплатно'} (мест: {event.max_seats})"
    )

    buttons = []
    if settings.WEBAPP_URL and settings.WEBAPP_URL.startswith("https://"):
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✨ Открыть встречу в Афише",
                    web_app=WebAppInfo(url=f"{settings.WEBAPP_URL}?event_id={event.id}"),
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="⚡ Записаться в 1 клик (с ТБ ознакомлен)", callback_data=f"book_{event.id}")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    sent_count = 0
    for plat in platforms:
        try:
            await bot.send_message(
                chat_id=int(plat.external_user_id),
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Не удалось доставить анонс {plat.external_user_id}: {e}")

    return sent_count


async def reminder_scheduler_loop():
    """Фоновый цикл: проверяет события каждые 60 секунд и шлет напоминания за 24 часа и за 2 часа"""
    while True:
        try:
            bot = get_bot()
            if bot:
                now = datetime.now()
                async with AsyncSessionLocal() as db:
                    stmt = (
                        select(Registration, User, Event, UserPlatform)
                        .join(User, Registration.user_id == User.id)
                        .join(Event, Registration.event_id == Event.id)
                        .join(UserPlatform, UserPlatform.user_id == User.id)
                        .where(
                            Event.status == EventStatus.PUBLISHED,
                            UserPlatform.platform == PlatformType.TG,
                            Registration.status.in_(
                                [RegistrationStatus.REGISTERED, RegistrationStatus.CONFIRMED]
                            ),
                        )
                    )
                    rows = (await db.execute(stmt)).all()

                    for reg, user, event, plat in rows:
                        hours_left = (event.starts_at - now).total_seconds() / 3600.0
                        ev_date, ev_time = format_event_datetime(event.starts_at)

                        # 1. Напоминание за 24 часа (окно: от 23.0 до 24.5 часов до старта)
                        if 23.0 <= hours_left <= 24.5 and reg.id not in sent_24h_reminders:
                            sent_24h_reminders.add(reg.id)
                            kb = InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [
                                        InlineKeyboardButton(
                                            text="🔥 Точно буду!", callback_data=f"conf_{reg.id}"
                                        ),
                                        InlineKeyboardButton(
                                            text="😔 Не смогу прийти", callback_data=f"cancel_{reg.id}"
                                        ),
                                    ]
                                ]
                            )
                            await bot.send_message(
                                chat_id=int(plat.external_user_id),
                                text=(
                                    f"🌿 <b>{user.first_name}, уже завтра встречаемся!</b>\n\n"
                                    f"Напоминаем, что вы записаны на <b>«{event.title}»</b>:\n"
                                    f"📅 Завтра ({ev_date}) в <b>{ev_time}</b>\n"
                                    f"📍 {event.address}\n\n"
                                    f"Подтвердите, пожалуйста, своё участие кнопкой ниже:"
                                ),
                                parse_mode="HTML",
                                reply_markup=kb,
                            )

                        # 2. Напоминание за 2 часа (окно: от 1.5 до 2.3 часов до старта)
                        elif 1.5 <= hours_left <= 2.3 and reg.id not in sent_2h_reminders:
                            sent_2h_reminders.add(reg.id)
                            place = (
                                f"{event.address} ({event.location_details})"
                                if event.location_details
                                else event.address
                            )
                            await bot.send_message(
                                chat_id=int(plat.external_user_id),
                                text=(
                                    f"☕️ <b>Скоро начинаем!</b>\n\n"
                                    f"Уже через 2 часа (в <b>{ev_time}</b>) ждём вас на встрече <b>«{event.title}»</b>.\n"
                                    f"📍 <b>Адрес:</b> {place}\n\n"
                                    f"До скорой встречи!"
                                ),
                                parse_mode="HTML",
                            )
        except Exception as e:
            logger.error(f"Ошибка планировщика напоминаний: {e}")

        await asyncio.sleep(60)


async def render_event_card(event_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with AsyncSessionLocal() as db:
        event = await db.get(Event, event_id)
        if not event or event.status != EventStatus.PUBLISHED:
            return "Встреча не найдена или запись уже закрыта.", main_menu_kb()

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
            f"✨ <b>{event.title}</b> ({event.kind})\n\n"
            f"{event.description}\n\n"
            f"📅 <b>Когда:</b> {ev_date} в {ev_time} ({event.duration or '2 часа'})\n"
            f"📍 <b>Где:</b> {place_str}\n"
            f"🎟 <b>Стоимость:</b> {event.price or 'Бесплатно'}\n"
            f"👥 <b>Свободно мест:</b> {avail} из {event.max_seats}"
            f"{safety_note}"
        )

        btn_text = (
            "✅ Записаться (с ТБ ознакомлен)"
            if avail > 0
            else "⏳ Встать в лист ожидания (с ТБ ознакомлен)"
        )
        buttons = []
        if settings.WEBAPP_URL and settings.WEBAPP_URL.startswith("https://"):
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="🗺 Открыть с картой в Mini App",
                        web_app=WebAppInfo(url=f"{settings.WEBAPP_URL}?event_id={event.id}"),
                    )
                ]
            )
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"book_{event.id}")])
        buttons.append([InlineKeyboardButton(text="⬅️ К списку встреч", callback_data="menu_events")])

        return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    await save_telegram_subscriber(str(message.from_user.id), message.from_user.username)

    args = command.args
    if args and args.startswith("event_"):
        try:
            event_id = int(args.split("_")[1])
            text, kb = await render_event_card(event_id)
            await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
            return
        except ValueError:
            pass

    welcome_text = (
        "🧡 <b>Приветствуем в творческом пространстве «Грея душу»!</b>\n\n"
        "Мы - творческое объединение из Тюмени, где рождаются уютные проекты и теплое общение."
        "Здесь вы можете:\n"
        "• 🪡 Узнать о предстоящих творческих и социальных мастер-классах.\n"
        "• 🌿 Записаться на встречи в пару кликов без лишних заполнений.\n"
        "• 📋 Управлять своими записями и всегда быть в курсе расписания.\n\n"
        "Нажмите кнопку <b>«Открыть Афишу»</b> ниже, чтобы выбрать душевную встречу для себя ✨"
    )
    await message.answer(welcome_text, reply_markup=main_menu_kb(), parse_mode="HTML")


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
                "Пока в афише тихо — новые мастер-классы появятся совсем скоро!",
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
            "Выберите встречу, чтобы узнать подробности и записаться:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data == "back_home")
async def back_home_handler(call: CallbackQuery):
    await call.message.edit_text(
        "🧡 <b>Главное меню</b>\nВыберите действие ниже:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("event_"))
async def open_event_handler(call: CallbackQuery):
    event_id = int(call.data.split("_")[1])
    text, kb = await render_event_card(event_id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@router.callback_query(F.data.startswith("conf_"))
async def confirm_attendance_handler(call: CallbackQuery):
    """Кнопка '🔥 Точно буду!' из напоминания за 24 часа"""
    reg_id = int(call.data.split("_")[1])
    async with AsyncSessionLocal() as db:
        reg = await db.get(Registration, reg_id)
        if reg and reg.status != RegistrationStatus.CANCELLED:
            reg.status = RegistrationStatus.CONFIRMED
            await db.commit()
            await call.message.edit_text(
                "🧡 <b>Спасибо, что подтвердили участие!</b>\nЖдем вас завтра!",
                parse_mode="HTML",
                reply_markup=main_menu_kb(),
            )
        else:
            await call.answer("Запись не найдена или была отменена", show_alert=True)


@router.callback_query(F.data.startswith("book_"))
async def book_event_handler(call: CallbackQuery, state: FSMContext):
    event_id = int(call.data.split("_")[1])
    tg_id = str(call.from_user.id)

    async with AsyncSessionLocal() as db:
        user = await crud.get_user_by_platform(db, PlatformType.TG, tg_id)

        if not user:
            await state.update_data(target_event_id=event_id)
            await state.set_state(OnboardingFSM.waiting_for_fio)
            await call.message.answer(
                "Для первой записи давай познакомимся (в следующий раз мы запомним тебя, и запись будет в 1 клик!).\n\n"
                "Напиши свои <b>Фамилию, Имя и Отчество</b> (отчество — при наличии) через пробел:",
                parse_mode="HTML",
            )
            await call.answer()
            return

        try:
            reg = await crud.book_event(
                db,
                RegistrationCreateSchema(user_id=user.id, event_id=event_id, safety_accepted=True),
            )
            event = await db.get(Event, event_id)
            ev_date, ev_time = format_event_datetime(event.starts_at)

            if reg.status == RegistrationStatus.REGISTERED:
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
                    f"Как только кто-то отменит запись на «{event.title}», мы сразу напишем вам."
                )

            await call.message.edit_text(msg, reply_markup=main_menu_kb(), parse_mode="HTML")
        except Exception as e:
            detail = getattr(e, "detail", str(e))
            await call.answer(f"⚠️ {detail}", show_alert=True)


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
        await message.answer("Кажется, в адресе почты опечатка. Попробуй еще раз:")
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
        "Последний шаг — согласие на обработку персональных данных:\n\n"
        f"• <a href='{settings.PRIVACY_POLICY_URL}'>Политика по обработке ПДн</a>\n"
        f"• <a href='{settings.PRIVACY_AGREEMENT_URL}'>Согласие на обработку ПДн</a>\n\n"
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
            status_label = (
                "✅ Записан"
                if reg.status in (RegistrationStatus.REGISTERED, RegistrationStatus.CONFIRMED)
                else "⏳ В очереди"
            )
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
                next_reg = (await db.execute(wait_stmt)).scalar_one_or_none()
                if next_reg:
                    next_reg.status = RegistrationStatus.REGISTERED
                    await db.commit()
                    next_user = await db.get(User, next_reg.user_id)
                    event = await db.get(Event, next_reg.event_id)
                    await send_confirmation_email(db, next_user, event, next_reg)
                    await notify_user_in_telegram(next_user, event, next_reg, db)

    await call.answer("Запись отменена", show_alert=True)
    await show_my_registrations(call)
