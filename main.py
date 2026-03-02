"""
AI Centers — Multi-Bot Demo Service
Запускает 6 демо-ботов параллельно, каждый со своим config.json и Gemini AI.
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import google.generativeai as genai

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("multibot")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "5309206282"))

genai.configure(api_key=GEMINI_API_KEY)

# Slug → env var suffix mapping
SLUG_TOKEN_MAP = {
    "restaurant": "RESTAURANT",
    "dental": "DENTAL",
    "beauty": "BEAUTY",
    "hotel": "HOTEL",
    "auto": "AUTO",
    "fitness": "FITNESS",
}


class Chat(StatesGroup):
    active = State()


def build_system_prompt(cfg: dict) -> str:
    """Build a rich system prompt from config.json."""
    name = cfg.get("business_name", "Бизнес")
    niche = cfg.get("niche", "other")
    desc = cfg.get("description", "")
    schedule = cfg.get("schedule", "Не указано")
    address = cfg.get("address", "Не указан")
    phone = cfg.get("phone", "Не указан")
    services = cfg.get("services", [])
    kb = cfg.get("knowledge_base", {})

    svc_lines = "\n".join(
        f"  • {s['name']} — {s.get('price','?')}  {s.get('description','')}"
        for s in services
    )

    features = "\n".join(f"  - {f}" for f in kb.get("features", []))
    promo = kb.get("promo", {})
    promo_text = (
        f"Акция: {promo['name']} — {promo['description']} (код: {promo.get('code','')})"
        if promo else ""
    )
    delivery = kb.get("delivery", {})
    delivery_text = (
        f"Доставка: {'Да' if delivery.get('available') else 'Нет'}, "
        f"бесплатно от {delivery.get('free_from','?')}, "
        f"время {delivery.get('time','?')}, зона: {delivery.get('area','?')}"
        if delivery else ""
    )
    booking = kb.get("booking", {})
    booking_text = (
        f"Запись/бронирование: {'Да' if booking.get('available') else 'Нет'}. "
        f"Макс. банкет: {booking.get('banquet_capacity','?')} чел."
        if booking else ""
    )

    return f"""Ты — AI-ассистент «{name}».
Ниша: {niche}. {desc}

Адрес: {address}
Телефон: {phone}
Расписание: {schedule}

Услуги / Меню:
{svc_lines}

{features}
{promo_text}
{delivery_text}
{booking_text}

Правила:
- Отвечай на языке клиента (по умолчанию русский).
- Будь дружелюбным, кратким и полезным.
- Если клиент хочет записаться — уточни дату, время, имя и телефон.
- Не выдумывай информацию, которой нет выше.
- Максимум 300 слов в ответе.
"""


def load_configs() -> list[dict]:
    """Load all demo bot configs."""
    demo_dir = os.getenv(
        "DEMO_CONFIGS",
        str(Path(__file__).resolve().parent.parent / "demo_bots"),
    )
    configs = []
    base = Path(demo_dir)
    for slug, env_suffix in SLUG_TOKEN_MAP.items():
        cfg_path = base / slug / "config.json"
        if not cfg_path.exists():
            logger.warning(f"Config not found: {cfg_path}")
            continue
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        token = os.getenv(f"BOT_TOKEN_{env_suffix}")
        if not token:
            logger.warning(f"No token for {slug} (BOT_TOKEN_{env_suffix}), skipping")
            continue
        cfg["_slug"] = slug
        cfg["_token"] = token
        configs.append(cfg)
    return configs


def make_services_text(cfg: dict) -> str:
    services = cfg.get("services", [])
    if not services:
        return "Список услуг пока не добавлен."
    lines = [f"📋 *{cfg.get('business_name', '')}* — Услуги:\n"]
    for s in services:
        d = f" — {s['description']}" if s.get("description") else ""
        lines.append(f"• *{s['name']}* — {s.get('price', '?')}{d}")
    return "\n".join(lines)


from aiogram.types import InputMediaPhoto


async def send_menu(message: Message, bot: Bot, cfg: dict):
    """Send menu with photos if available, otherwise text."""
    services = cfg.get("services", [])
    if not services:
        await message.answer("Список услуг пока не добавлен.")
        return

    # Services with photos — send as photo groups (max 10 per group)
    photo_services = [s for s in services if s.get("photo")]
    text_services = [s for s in services if not s.get("photo")]

    if photo_services:
        # Send in batches of 10 (Telegram limit for media groups)
        for i in range(0, len(photo_services), 10):
            batch = photo_services[i:i+10]
            media = []
            for s in batch:
                caption = f"*{s['name']}* — {s.get('price', '?')}"
                if s.get("description"):
                    caption += f"\n{s['description']}"
                media.append(InputMediaPhoto(
                    media=s["photo"],
                    caption=caption,
                    parse_mode="Markdown"
                ))
            try:
                await bot.send_media_group(message.chat.id, media)
            except Exception as e:
                logger.error(f"Media group error: {e}")
                # Fallback to text
                for s in batch:
                    await message.answer(f"*{s['name']}* — {s.get('price','?')}\n{s.get('description','')}", parse_mode="Markdown")

    # Remaining services without photos — text list
    if text_services:
        lines = [f"📋 *Ещё в меню:*\n"]
        for s in text_services:
            d = f" — {s['description']}" if s.get("description") else ""
            lines.append(f"• *{s['name']}* — {s.get('price', '?')}{d}")
        await message.answer("\n".join(lines), parse_mode="Markdown")


def create_bot_router(cfg: dict) -> Router:
    """Create an aiogram Router with handlers for one bot."""
    router = Router()
    slug = cfg["_slug"]
    bname = cfg.get("business_name", slug)
    system_prompt = build_system_prompt(cfg)
    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        system_instruction=system_prompt,
    )
    # Per-user chat sessions (user_id → genai ChatSession)
    chats: dict[int, genai.ChatSession] = {}

    def get_chat(user_id: int) -> genai.ChatSession:
        if user_id not in chats:
            chats[user_id] = model.start_chat(history=[])
        return chats[user_id]

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext, bot: Bot):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Услуги / Цены", callback_data="menu")],
            [InlineKeyboardButton(text="📝 Записаться", callback_data="book")],
            [InlineKeyboardButton(text="📞 Контакты", callback_data="contact")],
        ])
        await message.answer(
            f"Привет! 👋\nЯ AI-ассистент *{bname}*.\n\n"
            f"{cfg.get('description', '')}\n\n"
            f"🕒 {cfg.get('schedule', '')}\n"
            f"📍 {cfg.get('address', '')}\n\n"
            f"Задайте вопрос или выберите действие:",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        await state.set_state(Chat.active)

    @router.message(Command("menu"))
    async def cmd_menu(message: Message, bot: Bot, **kw):
        await send_menu(message, bot, cfg)

    @router.message(Command("book"))
    async def cmd_book(message: Message, **kw):
        await message.answer(
            "📝 Для записи укажите:\n"
            "• Желаемую услугу\n• Дату и время\n• Ваше имя\n• Телефон\n\n"
            "Или просто напишите, и я помогу!",
        )

    @router.message(Command("contact"))
    async def cmd_contact(message: Message, **kw):
        await message.answer(
            f"📞 *Контакты {bname}*\n\n"
            f"Телефон: {cfg.get('phone', 'Не указан')}\n"
            f"Адрес: {cfg.get('address', 'Не указан')}\n"
            f"Расписание: {cfg.get('schedule', '')}",
            parse_mode="Markdown",
        )

    @router.callback_query(F.data == "menu")
    async def cb_menu(cb: CallbackQuery, bot: Bot):
        await send_menu(cb.message, bot, cfg)
        await cb.answer()

    @router.callback_query(F.data == "book")
    async def cb_book(cb: CallbackQuery):
        await cb.message.answer(
            "📝 Для записи укажите желаемую услугу, дату/время, имя и телефон."
        )
        await cb.answer()

    @router.callback_query(F.data == "contact")
    async def cb_contact(cb: CallbackQuery):
        await cb.message.answer(
            f"📞 {cfg.get('phone','')}\n📍 {cfg.get('address','')}\n🕒 {cfg.get('schedule','')}",
        )
        await cb.answer()

    @router.message(F.voice)
    async def handle_voice(message: Message, state: FSMContext, bot: Bot):
        """Transcribe voice via Gemini upload_file, then respond."""
        try:
            file = await bot.get_file(message.voice.file_id)
            buf = await bot.download_file(file.file_path)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp.write(buf.read())
                tmp_path = tmp.name

            uploaded = genai.upload_file(tmp_path, mime_type="audio/ogg")
            transcript_resp = genai.GenerativeModel("gemini-2.5-flash").generate_content([
                "Транскрибируй это голосовое сообщение. Верни только текст, без пояснений.",
                uploaded,
            ])
            os.unlink(tmp_path)
            text = transcript_resp.text.strip()
            if not text:
                await message.answer("Не удалось распознать голосовое сообщение.")
                return

            await bot.send_chat_action(message.chat.id, "typing")
            chat = get_chat(message.from_user.id)
            resp = await asyncio.to_thread(chat.send_message, text)
            answer = resp.text

            await message.answer(f"🎤 _{text}_\n\n{answer}", parse_mode="Markdown")
            await _notify_owner(bot, message, text, answer)
        except Exception as e:
            logger.error(f"[{slug}] voice error: {e}")
            await message.answer("Ошибка при обработке голосового сообщения.")

    @router.message(F.text)
    async def handle_text(message: Message, state: FSMContext, bot: Bot):
        uid = message.from_user.id
        text = message.text
        await bot.send_chat_action(message.chat.id, "typing")
        try:
            chat = get_chat(uid)
            resp = await asyncio.to_thread(chat.send_message, text)
            answer = resp.text
        except Exception as e:
            logger.error(f"[{slug}] gemini error: {e}")
            answer = "Извините, произошла ошибка. Попробуйте позже."
        await message.answer(answer)
        await _notify_owner(bot, message, text, answer)

    async def _notify_owner(bot: Bot, message: Message, question: str, answer: str):
        uid = message.from_user.id
        if OWNER_TELEGRAM_ID and uid != OWNER_TELEGRAM_ID:
            try:
                await bot.send_message(
                    OWNER_TELEGRAM_ID,
                    f"💬 [{bname}] @{message.from_user.username or uid}:\n"
                    f"Q: {question[:300]}\nA: {answer[:300]}",
                )
            except Exception:
                pass

    return router


async def run_bot(cfg: dict):
    """Run a single bot polling loop."""
    slug = cfg["_slug"]
    token = cfg["_token"]
    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(create_bot_router(cfg))
    logger.info(f"Starting bot [{slug}] — {cfg.get('business_name')}")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot [{slug}] crashed: {e}")


async def main():
    configs = load_configs()
    if not configs:
        logger.error("No bots configured. Set BOT_TOKEN_* env vars and check config paths.")
        return
    logger.info(f"Loaded {len(configs)} bot(s): {[c['_slug'] for c in configs]}")
    await asyncio.gather(*(run_bot(c) for c in configs))


if __name__ == "__main__":
    asyncio.run(main())
