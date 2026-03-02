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


class UploadPhoto(StatesGroup):
    waiting_photo = State()


# Persistent photo storage (file_id per bot per dish)
# Structure: { "bot_slug": { "dish_name": "telegram_file_id" } }
PHOTOS_PATH = os.getenv("PHOTOS_DB", "/app/photos.json")


def load_photos() -> dict:
    try:
        with open(PHOTOS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_photos(data: dict):
    with open(PHOTOS_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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

    slug = cfg.get("_slug", "")
    photos_db = load_photos()
    bot_photos = photos_db.get(slug, {})

    photo_services = []
    text_services = []
    for s in services:
        if s["name"] in bot_photos:
            s["_file_id"] = bot_photos[s["name"]]
            photo_services.append(s)
        elif s.get("photo"):
            photo_services.append(s)
        else:
            text_services.append(s)

    if photo_services:
        for s in photo_services:
            caption = f"<b>{s['name']}</b> — {s.get('price', '?')}"
            if s.get("description"):
                caption += f"\n{s['description']}"
            photo = s.get("_file_id") or s.get("photo", "")
            try:
                await bot.send_photo(
                    message.chat.id,
                    photo=photo,
                    caption=caption,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Photo send error for {s['name']}: {e}")
                await message.answer(f"{s['name']} — {s.get('price','?')}\n{s.get('description','')}")

    if text_services:
        lines = ["📋 <b>Ещё в меню:</b>\n"]
        for s in text_services:
            d = f" — {s['description']}" if s.get("description") else ""
            lines.append(f"• <b>{s['name']}</b> — {s.get('price', '?')}{d}")
        await message.answer("\n".join(lines), parse_mode="HTML")


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
    # Per-user conversation history (simple text pairs)
    histories: dict[int, list[dict]] = {}

    def get_history(user_id: int) -> list[dict]:
        if user_id not in histories:
            histories[user_id] = []
        return histories[user_id]

    async def ask_gemini(user_id: int, text: str) -> str:
        """Send message to Gemini with conversation history."""
        history = get_history(user_id)
        # Build contents with history
        contents = []
        for h in history[-8:]:  # last 8 messages for context
            contents.append({"role": h["role"], "parts": [{"text": h["text"]}]})
        contents.append({"role": "user", "parts": [{"text": text}]})

        resp = await asyncio.to_thread(
            model.generate_content, contents
        )
        answer = resp.text

        # Save to history
        history.append({"role": "user", "text": text})
        history.append({"role": "model", "text": answer})
        # Keep history manageable
        if len(history) > 20:
            histories[user_id] = history[-20:]

        return answer

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

    @router.message(Command("upload"))
    async def cmd_upload(message: Message, state: FSMContext, **kw):
        """Owner uploads photos for menu items."""
        if message.from_user.id != OWNER_TELEGRAM_ID:
            await message.answer("⛔ Только владелец может загружать фото.")
            return
        services = cfg.get("services", [])
        if not services:
            await message.answer("Нет услуг в конфиге.")
            return
        # Show numbered list of dishes
        photos_db = load_photos()
        bot_photos = photos_db.get(slug, {})
        lines = [f"📸 <b>Загрузка фото для {bname}</b>\n\nВыберите блюдо (отправьте номер):\n"]
        for i, s in enumerate(services, 1):
            has_photo = "✅" if s["name"] in bot_photos else "❌"
            lines.append(f"{i}. {has_photo} {s['name']} — {s.get('price', '?')}")
        lines.append(f"\n📌 Отправьте номер от 1 до {len(services)}")
        await message.answer("\n".join(lines), parse_mode="HTML")
        await state.set_state(UploadPhoto.waiting_photo)
        await state.update_data(upload_step="select")

    @router.message(UploadPhoto.waiting_photo, F.text)
    async def upload_select(message: Message, state: FSMContext, **kw):
        """User selects dish number."""
        data = await state.get_data()
        services = cfg.get("services", [])

        if data.get("upload_step") == "select":
            try:
                idx = int(message.text.strip()) - 1
                if 0 <= idx < len(services):
                    dish = services[idx]["name"]
                    await state.update_data(upload_step="photo", dish_name=dish)
                    await message.answer(f"📷 Отправьте фото для <b>{dish}</b>", parse_mode="HTML")
                else:
                    await message.answer(f"Номер от 1 до {len(services)}")
            except ValueError:
                await message.answer("Отправьте номер блюда")
        else:
            await state.clear()
            await message.answer("Загрузка отменена.")

    @router.message(UploadPhoto.waiting_photo, F.photo)
    async def upload_photo(message: Message, state: FSMContext, **kw):
        """Receive photo and save file_id."""
        data = await state.get_data()
        dish_name = data.get("dish_name")
        if not dish_name:
            await message.answer("Сначала выберите блюдо через /upload")
            await state.clear()
            return

        file_id = message.photo[-1].file_id  # Best quality
        photos_db = load_photos()
        if slug not in photos_db:
            photos_db[slug] = {}
        photos_db[slug][dish_name] = file_id
        save_photos(photos_db)

        await state.clear()
        await message.answer(
            f"✅ Фото для <b>{dish_name}</b> сохранено!\n\n"
            f"Загрузить ещё? Нажмите /upload",
            parse_mode="HTML"
        )

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
            answer = await ask_gemini(message.from_user.id, text)

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
            answer = await ask_gemini(uid, text)
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
