"""Каталог доступных голосов (edge-tts / Microsoft Neural).

Все голоса бесплатные, звучат естественно. Полный список — командой
`edge-tts --list-voices`.

Про русские мужские голоса: родной русский мужской голос в edge-tts один —
Дмитрий. Дополнительно добавлены новые «мультиязычные» голоса (Multilingual) —
это самое реалистичное поколение, звучат максимально по-человечески и умеют
читать русский текст. Попробуй разные и выбери, что нравится.
"""

# Ключ — короткий id для кнопок, значение — данные голоса.
VOICES = {
    # --- Русские (родные) ---
    "ru_dmitry":   {"name": "🇷🇺 Дмитрий (муж.)",        "id": "ru-RU-DmitryNeural"},
    "ru_svetlana": {"name": "🇷🇺 Светлана (жен.)",       "id": "ru-RU-SvetlanaNeural"},

    # --- Мужские реалистичные (мультиязычные, читают по-русски) ---
    "ml_andrew":   {"name": "🎙 Эндрю (муж., реалистичный)",  "id": "en-US-AndrewMultilingualNeural"},
    "ml_brian":    {"name": "🎙 Брайан (муж., реалистичный)", "id": "en-US-BrianMultilingualNeural"},
    "ml_florian":  {"name": "🎙 Флориан (муж., реалистичный)","id": "de-DE-FlorianMultilingualNeural"},
    "ml_remy":     {"name": "🎙 Реми (муж., реалистичный)",   "id": "fr-FR-RemyMultilingualNeural"},

    # --- Женские реалистичные (мультиязычные) ---
    "ml_ava":      {"name": "🎙 Ава (жен., реалистичная)",    "id": "en-US-AvaMultilingualNeural"},
    "ml_emma":     {"name": "🎙 Эмма (жен., реалистичная)",   "id": "en-US-EmmaMultilingualNeural"},

    # --- Английские (US/UK) ---
    "en_guy":      {"name": "🇺🇸 Guy (муж.)",            "id": "en-US-GuyNeural"},
    "en_chris":    {"name": "🇺🇸 Christopher (муж.)",    "id": "en-US-ChristopherNeural"},
    "en_ryan":     {"name": "🇬🇧 Ryan (муж.)",           "id": "en-GB-RyanNeural"},
    "en_aria":     {"name": "🇺🇸 Aria (жен.)",           "id": "en-US-AriaNeural"},
}

DEFAULT_VOICE = "ru_dmitry"
