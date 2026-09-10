"""
Telegram-бот для озвучки видео.

Бесплатно, без API-ключей и лимитов — использует нейроголоса Microsoft Edge
(edge-tts). Пишешь боту текст — получаешь готовый mp3 для видео.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN="токен от @BotFather"
    python bot.py
"""

import asyncio
import json
import logging
import os
import re
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
    Message,
)

from voices import DEFAULT_VOICE, VOICES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voiceover-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Файл, где храним выбор голоса/скорости для каждого пользователя (переживает
# перезапуск бота).
PREFS_FILE = Path(__file__).parent / "user_prefs.json"

# Ограничение на длину текста за один раз (edge-tts тянет и больше, но так
# безопаснее и быстрее).
MAX_CHARS = 5000


# ---------------------------------------------------------------------------
# Хранилище пользовательских настроек
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
    p.setdefault("rate", "+0%")   # скорость: -50%..+100%
    p.setdefault("pitch", "+0Hz") # тон: например -20Hz / +20Hz
    return p


# ---------------------------------------------------------------------------
# Синтез речи
# ---------------------------------------------------------------------------
async def synthesize(text: str, voice_id: str, rate: str, pitch: str) -> bytes:
    """Возвращает mp3 в виде байтов."""
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
def voices_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for key, v in VOICES.items():
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(text=mark + v["name"], callback_data=f"voice:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        "<b>Команды:</b>\n"
        "• /voice — выбрать голос (русский / английский)\n"
        "• /rate <code>+10%</code> — скорость (от -50% до +100%)\n"
        "• /pitch <code>+0Hz</code> — тон голоса (например -15Hz)\n"
        "• /settings — текущие настройки",
        parse_mode="HTML",
    )


@dp.message(Command("voice"))
async def cmd_voice(message: Message) -> None:
    p = get_pref(message.from_user.id)
    await message.answer("🎙 Выбери голос:", reply_markup=voices_keyboard(p["voice"]))


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


@dp.message(Command("rate"))
async def cmd_rate(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи скорость, например: <code>/rate +15%</code>", parse_mode="HTML")
        return
    val = parts[1].strip()
    if not re.fullmatch(r"[+-]\d{1,3}%", val):
        await message.answer("Формат: <code>/rate +15%</code> или <code>/rate -20%</code>", parse_mode="HTML")
        return
    p = get_pref(message.from_user.id)
    p["rate"] = val
    _save_prefs(prefs)
    await message.answer(f"⚡ Скорость: <b>{val}</b>", parse_mode="HTML")


@dp.message(Command("pitch"))
async def cmd_pitch(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи тон, например: <code>/pitch -15Hz</code>", parse_mode="HTML")
        return
    val = parts[1].strip()
    if not re.fullmatch(r"[+-]\d{1,3}Hz", val):
        await message.answer("Формат: <code>/pitch -15Hz</code> или <code>/pitch +10Hz</code>", parse_mode="HTML")
        return
    p = get_pref(message.from_user.id)
    p["pitch"] = val
    _save_prefs(prefs)
    await message.answer(f"🎚 Тон: <b>{val}</b>", parse_mode="HTML")


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    p = get_pref(message.from_user.id)
    voice_name = VOICES.get(p["voice"], VOICES[DEFAULT_VOICE])["name"]
    await message.answer(
        f"⚙️ <b>Настройки:</b>\n"
        f"🎙 Голос: <b>{voice_name}</b>\n"
        f"⚡ Скорость: <b>{p['rate']}</b>\n"
        f"🎚 Тон: <b>{p['pitch']}</b>",
        parse_mode="HTML",
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    text = message.text.strip()
    if not text:
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

    filename = "voiceover.mp3"
    caption = f"🎙 {voice['name']}"
    file = BufferedInputFile(audio, filename=filename)
    # Отправляем и как аудио (слушать), и файлом озвучка удобно скачивается.
    await message.answer_audio(audio=file, caption=caption, title="Озвучка")
    await status.delete()


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Получи токен у @BotFather в Telegram и запусти:\n"
            '  export BOT_TOKEN="123:abc"\n  python bot.py'
        )
    bot = Bot(BOT_TOKEN)
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
