"""
Telegram-бот для озвучки видео.

Бесплатно, без API-ключей и лимитов — использует нейроголоса Microsoft Edge
(edge-tts). Пишешь боту текст — получаешь готовый mp3 для видео.

Дополнительно (необязательно): клонирование своего голоса на XTTS v2 —
записываешь образец, бот озвучивает текст твоим голосом. Включается после
установки пакетов из requirements-clone.txt (см. README).

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

import cloning
import mediautil
from voices import DEFAULT_VOICE, VOICES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voiceover-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

BASE_DIR = Path(__file__).parent
PREFS_FILE = BASE_DIR / "user_prefs.json"
SAMPLES_DIR = BASE_DIR / "voice_samples"
SAMPLES_DIR.mkdir(exist_ok=True)
MAX_CHARS = 5000

CLONE_VOICE_KEY = "my_voice"

# Тексты кнопок нижнего меню
BTN_VOICE = "🎙 Голос"
BTN_CLONE = "🎤 Мой голос"
BTN_RATE = "⚡ Скорость"
BTN_PITCH = "🎚 Тон"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "ℹ️ Помощь"
MENU_BUTTONS = {BTN_VOICE, BTN_CLONE, BTN_RATE, BTN_PITCH, BTN_SETTINGS, BTN_HELP}

RATE_PRESETS = ["-25%", "-10%", "+0%", "+10%", "+25%", "+50%"]
PITCH_PRESETS = ["-25Hz", "-10Hz", "+0Hz", "+10Hz", "+25Hz"]

# Текст-скрипт, который просим прочитать для образца голоса
SAMPLE_SCRIPT = (
    "Привет! Меня зовут, и я записываю образец своего голоса. "
    "Я говорю спокойно и чётко, чтобы бот хорошо запомнил моё звучание. "
    "Сегодня отличный день, чтобы озвучить видео своим собственным голосом."
)

# Пользователи, от которых сейчас ждём образец голоса
awaiting_sample: set[int] = set()


# ---------------------------------------------------------------------------
# Хранилище настроек
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


def sample_path(user_id: int) -> Path:
    return SAMPLES_DIR / f"{user_id}.wav"


def has_sample(user_id: int) -> bool:
    return sample_path(user_id).exists()


def detect_lang(text: str) -> str:
    """Грубое определение языка для XTTS: есть кириллица -> ru, иначе en."""
    return "ru" if any("а" <= c.lower() <= "я" or c in "ёЁ" for c in text) else "en"


# ---------------------------------------------------------------------------
# Синтез (edge-tts)
# ---------------------------------------------------------------------------
async def synthesize_edge(text: str, voice_id: str, rate: str, pitch: str) -> bytes:
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
            [KeyboardButton(text=BTN_VOICE), KeyboardButton(text=BTN_CLONE)],
            [KeyboardButton(text=BTN_RATE), KeyboardButton(text=BTN_PITCH)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Пришли текст для озвучки…",
    )


def back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]


def voices_keyboard(user_id: int, current: str) -> InlineKeyboardMarkup:
    rows = []
    # Клонированный голос сверху, если образец записан
    if has_sample(user_id):
        mark = "✅ " if current == CLONE_VOICE_KEY else ""
        rows.append([InlineKeyboardButton(
            text=mark + "🎤 Мой голос (клон)", callback_data=f"voice:{CLONE_VOICE_KEY}")])
    for key, v in VOICES.items():
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(text=mark + v["name"], callback_data=f"voice:{key}")])
    rows.append(back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preset_keyboard(presets: list[str], current: str, prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for val in presets:
        mark = "✅ " if val == current else ""
        row.append(InlineKeyboardButton(text=mark + val, callback_data=f"{prefix}:{val}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rate_keyboard(current: str) -> InlineKeyboardMarkup:
    return _preset_keyboard(RATE_PRESETS, current, "rate")


def pitch_keyboard(current: str) -> InlineKeyboardMarkup:
    return _preset_keyboard(PITCH_PRESETS, current, "pitch")


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Отмена", callback_data="cancel_sample")]])


HELP_TEXT = (
    "ℹ️ <b>Как пользоваться</b>\n\n"
    "1. <b>🎙 Голос</b> — выбрать готовый голос (русский / английский / реалистичные).\n"
    "2. <b>🎤 Мой голос</b> — записать образец и озвучивать текст <b>своим</b> голосом (клон).\n"
    "3. <b>⚡ Скорость</b> и <b>🎚 Тон</b> — подстроить звучание (для готовых голосов).\n"
    "4. Просто пришли <b>текст</b> — получишь готовый <b>mp3</b>.\n\n"
    "💡 Для клона запиши <b>чистое</b> голосовое на 15–30 секунд, без шума и музыки."
)


# ---------------------------------------------------------------------------
# Бот
# ---------------------------------------------------------------------------
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    p = get_pref(message.from_user.id)
    voice_name = current_voice_name(message.from_user.id, p)
    await message.answer(
        "👋 <b>Привет! Я озвучу твой текст реалистичным голосом.</b>\n\n"
        "Просто пришли мне текст — верну готовый <b>mp3</b> для видео.\n\n"
        f"🎙 Текущий голос: <b>{voice_name}</b>\n\n"
        "Пользуйся кнопками меню внизу 👇",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


def current_voice_name(user_id: int, p: dict) -> str:
    if p["voice"] == CLONE_VOICE_KEY:
        return "🎤 Мой голос (клон)"
    return VOICES.get(p["voice"], VOICES[DEFAULT_VOICE])["name"]


# ---- Кнопки нижнего меню ----
@dp.message(F.text == BTN_VOICE)
async def open_voice(message: Message) -> None:
    p = get_pref(message.from_user.id)
    await message.answer("🎙 Выбери голос:", reply_markup=voices_keyboard(message.from_user.id, p["voice"]))


@dp.message(F.text == BTN_CLONE)
async def open_clone(message: Message) -> None:
    uid = message.from_user.id
    if not cloning.available():
        await message.answer(
            "🎤 <b>Клонирование голоса пока не установлено.</b>\n\n"
            "Чтобы включить, установи пакеты (один раз):\n"
            "<code>pip install torch --index-url https://download.pytorch.org/whl/cu121</code>\n"
            "<code>pip install -r requirements-clone.txt</code>\n\n"
            "Подробности — в файле README (раздел «Клонирование голоса»).",
            parse_mode="HTML",
        )
        return
    awaiting_sample.add(uid)
    status = "✅ образец уже записан, можешь перезаписать" if has_sample(uid) else "образец ещё не записан"
    await message.answer(
        "🎤 <b>Запись своего голоса</b>\n\n"
        f"Статус: {status}.\n\n"
        "1. Нажми и держи 🎤 (запись голосового).\n"
        "2. Прочитай <b>чётко и без шума</b> вот этот текст (15–30 сек):\n\n"
        f"<i>{SAMPLE_SCRIPT}</i>\n\n"
        "3. Отправь голосовое сообщение мне.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


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
    await message.answer(
        f"⚙️ <b>Настройки:</b>\n"
        f"🎙 Голос: <b>{current_voice_name(message.from_user.id, p)}</b>\n"
        f"⚡ Скорость: <b>{p['rate']}</b>\n"
        f"🎚 Тон: <b>{p['pitch']}</b>\n"
        f"🎤 Клон: <b>{'записан' if has_sample(message.from_user.id) else 'нет'}</b>",
        parse_mode="HTML",
    )


@dp.message(F.text == BTN_HELP)
async def open_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


# ---- Инлайн-кнопки ----
@dp.callback_query(F.data == "back")
async def on_back(cb: CallbackQuery) -> None:
    await cb.message.delete()
    await cb.answer()


@dp.callback_query(F.data == "cancel_sample")
async def on_cancel_sample(cb: CallbackQuery) -> None:
    awaiting_sample.discard(cb.from_user.id)
    await cb.message.delete()
    await cb.answer("Отменено")


@dp.callback_query(F.data.startswith("voice:"))
async def on_voice_chosen(cb: CallbackQuery) -> None:
    key = cb.data.split(":", 1)[1]
    if key == CLONE_VOICE_KEY:
        if not has_sample(cb.from_user.id):
            await cb.answer("Сначала запиши образец кнопкой 🎤 Мой голос", show_alert=True)
            return
    elif key not in VOICES:
        await cb.answer("Неизвестный голос")
        return
    p = get_pref(cb.from_user.id)
    p["voice"] = key
    _save_prefs(prefs)
    await cb.message.edit_reply_markup(reply_markup=voices_keyboard(cb.from_user.id, key))
    await cb.answer(f"Голос: {current_voice_name(cb.from_user.id, p)}")


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


# ---- Приём голосового образца ----
@dp.message(F.voice | F.audio)
async def on_voice_sample(message: Message) -> None:
    uid = message.from_user.id
    if uid not in awaiting_sample:
        await message.answer(
            "🎤 Чтобы записать свой голос, сначала нажми кнопку <b>🎤 Мой голос</b> в меню.",
            parse_mode="HTML",
        )
        return

    status = await message.answer("⏳ Обрабатываю образец голоса…")
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "sample_raw")
        tg_file = await message.bot.get_file(file_id)
        await message.bot.download_file(tg_file.file_path, destination=raw)
        try:
            mediautil.to_wav(raw, str(sample_path(uid)))
        except Exception as e:  # noqa: BLE001
            log.exception("sample convert failed")
            await status.edit_text(f"⚠️ Не удалось обработать запись: {e}")
            return

    awaiting_sample.discard(uid)
    p = get_pref(uid)
    p["voice"] = CLONE_VOICE_KEY
    _save_prefs(prefs)
    await status.edit_text(
        "✅ <b>Голос записан!</b>\n\nТеперь пришли любой текст — озвучу его твоим голосом.\n"
        "Первая озвучка может быть медленной (загружается модель), дальше быстрее.",
        parse_mode="HTML",
    )


# ---- Основной обработчик текста ----
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

    if p["voice"] == CLONE_VOICE_KEY:
        await voiceover_clone(message, text)
    else:
        await voiceover_edge(message, text, p)


async def voiceover_edge(message: Message, text: str, p: dict) -> None:
    voice = VOICES.get(p["voice"], VOICES[DEFAULT_VOICE])
    status = await message.answer("🎧 Озвучиваю…")
    try:
        audio = await synthesize_edge(text, voice["id"], p["rate"], p["pitch"])
    except Exception as e:  # noqa: BLE001
        log.exception("edge synthesis failed")
        await status.edit_text(f"⚠️ Не получилось озвучить: {e}")
        return
    file = BufferedInputFile(audio, filename="voiceover.mp3")
    await message.answer_audio(audio=file, caption=f"🎙 {voice['name']}", title="Озвучка")
    await status.delete()


async def voiceover_clone(message: Message, text: str) -> None:
    uid = message.from_user.id
    if not cloning.available():
        await message.answer("🎤 Клонирование не установлено. Нажми «🎤 Мой голос» — там инструкция.")
        return
    if not has_sample(uid):
        await message.answer("🎤 Сначала запиши образец: кнопка «🎤 Мой голос».")
        return

    first = not cloning.is_loaded()
    status = await message.answer(
        "🎙 Озвучиваю твоим голосом…" + (
            "\n⏳ Первый раз загружаю модель (~2 ГБ), это может занять несколько минут." if first else ""
        )
    )
    lang = detect_lang(text)
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "out.wav")
        mp3 = os.path.join(tmp, "out.mp3")
        try:
            await cloning.synthesize_clone(text, str(sample_path(uid)), lang, wav)
            mediautil.to_mp3(wav, mp3)
            audio = Path(mp3).read_bytes()
        except Exception as e:  # noqa: BLE001
            log.exception("clone synthesis failed")
            await status.edit_text(f"⚠️ Не получилось озвучить твоим голосом: {e}")
            return
    file = BufferedInputFile(audio, filename="voiceover.mp3")
    await message.answer_audio(audio=file, caption="🎤 Твой голос", title="Озвучка")
    await status.delete()


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Получи токен у @BotFather и запусти:\n"
            '  $env:BOT_TOKEN="123:abc"   (PowerShell)\n  python bot.py'
        )
    bot = Bot(BOT_TOKEN)
    log.info("Bot started (клонирование: %s)", "включено" if cloning.available() else "выключено")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
