"""
Telegram-бот для озвучки видео.

Бесплатно, без API-ключей и лимитов — использует нейроголоса Microsoft Edge
(edge-tts). Пишешь боту текст — получаешь готовый mp3 для видео.

Запуск:
    pip install -r requirements.txt
    $env:BOT_TOKEN="токен от @BotFather"   # PowerShell
    python bot.py
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

import edge_tts
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from voices import DEFAULT_VOICE, VOICES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voiceover-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

PREFS_FILE = Path(__file__).parent / "user_prefs.json"
MAX_CHARS = 5000

# Тексты кнопок нижнего меню
BTN_VOICE = "🎙 Голос"
BTN_RATE = "⚡ Скорость"
BTN_PITCH = "🎚 Тон"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "ℹ️ Помощь"
MENU_BUTTONS = {BTN_VOICE, BTN_RATE, BTN_PITCH, BTN_SETTINGS, BTN_HELP}

# Пресеты скорости и тона (значение edge-tts -> подпись на кнопке)
RATE_PRESETS = ["-25%", "-10%", "+0%", "+10%", "+25%", "+50%"]
PITCH_PRESETS = ["-25Hz", "-10Hz", "+0Hz", "+10Hz", "+25Hz"]


# ---------------------------------------------------------------------------
# Хранилище настроек пользователей
# ---------------------------------------------------------------------------
def _load_prefs() -> dict:
    try:
        return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_prefs(prefs: dict) -> None:
    PREFS_FILE.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")


prefs: dict = _load_prefs()


def get_pref(user_id: int) -> dict:
    p = prefs.setdefault(str(user_id), {})
    p.setdefault("voice", DEFAULT_VOICE)
    p.setdefault("rate", "+0%")
    p.setdefault("pitch", "+0Hz")
    return p


# ---------------------------------------------------------------------------
# Синтез речи
# ---------------------------------------------------------------------------
async def synthesize(text: str, voice_id: str, rate: str, pitch: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice_id, rate=rate, pitch=pitch)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await communicate.save(tmp_path)
        return Path(tmp_path).read_bytes()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_VOICE), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_RATE), KeyboardButton(text=BTN_PITCH)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Пришли текст для озвучки…",
    )


def back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]


def voices_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for key, v in VOICES.items():
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(text=mark + v["name"], callback_data=f"voice:{key}")])
    rows.append(back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rate_keyboard(current: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for val in RATE_PRESETS:
        mark = "✅ " if val == current else ""
        row.append(InlineKeyboardButton(text=mark + val, callback_data=f"rate:{val}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pitch_keyboard(current: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for val in PITCH_PRESETS:
        mark = "✅ " if val == current else ""
        row.append(InlineKeyboardButton(text=mark + val, callback_data=f"pitch:{val}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


HELP_TEXT = (
    "ℹ️ <b>Как пользоваться</b>\n\n"
    "1. Кнопка <b>🎙 Голос</b> — выбрать голос (русский / английский / реалистичные мужские).\n"
    "2. <b>⚡ Скорость</b> и <b>🎚 Тон</b> — подстроить звучание.\n"
    "3. Просто пришли <b>текст</b> — получишь готовый <b>mp3</b> для видео.\n\n"
    "💡 Для русского попробуй голоса «реалистичный» (Эндрю, Брайан) — они "
    "звучат максимально по-человечески."
)


# ---------------------------------------------------------------------------
# Бот
# ---------------------------------------------------------------------------
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    p = get_pref(message.from_user.id)
    voice_name = VOICES.get(p["voice"], VOICES[DEFAULT_VOICE])["name"]
    await message.answer(
        "👋 <b>Привет! Я озвучу твой текст реалистичным голосом.</b>\n\n"
        "Просто пришли мне текст — верну готовый <b>mp3</b> для видео.\n\n"
        f"🎙 Текущий голос: <b>{voice_name}</b>\n\n"
        "Пользуйся кнопками меню внизу 👇",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# ---- Кнопки нижнего меню (текстовые) ----
@dp.message(F.text == BTN_VOICE)
async def open_voice(message: Message) -> None:
    p = get_pref(message.from_user.id)
    await message.answer("🎙 Выбери голос:", reply_markup=voices_keyboard(p["voice"]))


@dp.message(F.text == BTN_RATE)
async def open_rate(message: Message) -> None:
    p = get_pref(message.from_user.id)
    await message.answer("⚡ Выбери скорость чтения:", reply_markup=rate_keyboard(p["rate"]))


@dp.message(F.text == BTN_PITCH)
async def open_pitch(message: Message) -> None:
    p = get_pref(message.from_user.id)
    await message.answer("🎚 Выбери тон голоса:", reply_markup=pitch_keyboard(p["pitch"]))


@dp.message(F.text == BTN_SETTINGS)
async def open_settings(message: Message) -> None:
    p = get_pref(message.from_user.id)
    voice_name = VOICES.get(p["voice"], VOICES[DEFAULT_VOICE])["name"]
    await message.answer(
        f"⚙️ <b>Настройки:</b>\n"
        f"🎙 Голос: <b>{voice_name}</b>\n"
        f"⚡ Скорость: <b>{p['rate']}</b>\n"
        f"🎚 Тон: <b>{p['pitch']}</b>",
        parse_mode="HTML",
    )


@dp.message(F.text == BTN_HELP)
async def open_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


# ---- Инлайн-кнопки (выбор + Назад) ----
@dp.callback_query(F.data == "back")
async def on_back(cb: CallbackQuery) -> None:
    await cb.message.delete()
    await cb.answer()


@dp.callback_query(F.data.startswith("voice:"))
async def on_voice_chosen(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 1)[1]
    if key not in VOICES:
        await cb.answer("Неизвестный голос")
        return
    p = get_pref(cb.from_user.id)
    p["voice"] = key
    _save_prefs(prefs)
    await cb.message.edit_reply_markup(reply_markup=voices_keyboard(key))
    await cb.answer(f"Голос: {VOICES[key]['name']}")


@dp.callback_query(F.data.startswith("rate:"))
async def on_rate_chosen(cb: CallbackQuery) -> None:
    val = cb.data.split(":", 1)[1]
    p = get_pref(cb.from_user.id)
    p["rate"] = val
    _save_prefs(prefs)
    await cb.message.edit_reply_markup(reply_markup=rate_keyboard(val))
    await cb.answer(f"Скорость: {val}")


@dp.callback_query(F.data.startswith("pitch:"))
async def on_pitch_chosen(cb: CallbackQuery) -> None:
    val = cb.data.split(":", 1)[1]
    p = get_pref(cb.from_user.id)
    p["pitch"] = val
    _save_prefs(prefs)
    await cb.message.edit_reply_markup(reply_markup=pitch_keyboard(val))
    await cb.answer(f"Тон: {val}")


# ---- Основной обработчик текста -> озвучка ----
@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    text = message.text.strip()
    if not text or text in MENU_BUTTONS:
        return
    if len(text) > MAX_CHARS:
        await message.answer(
            f"❗️ Текст слишком длинный ({len(text)} символов). "
            f"Максимум {MAX_CHARS} за раз — раздели на части."
        )
        return

    p = get_pref(message.from_user.id)
    voice = VOICES.get(p["voice"], VOICES[DEFAULT_VOICE])
    status = await message.answer("🎧 Озвучиваю…")

    try:
        audio = await synthesize(text, voice["id"], p["rate"], p["pitch"])
    except Exception as e:  # noqa: BLE001
        log.exception("synthesis failed")
        await status.edit_text(f"⚠️ Не получилось озвучить: {e}")
        return

    file = BufferedInputFile(audio, filename="voiceover.mp3")
    await message.answer_audio(audio=file, caption=f"🎙 {voice['name']}", title="Озвучка")
    await status.delete()


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Получи токен у @BotFather и запусти:\n"
            '  $env:BOT_TOKEN="123:abc"   (PowerShell)\n  python bot.py'
        )
    bot = Bot(BOT_TOKEN)
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
