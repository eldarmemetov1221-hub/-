"""Каталог доступных голосов (edge-tts / Microsoft Neural).

Все голоса бесплатные, звучат естественно. Можно добавить любые из полного
списка командой `edge-tts --list-voices`.
"""

# Ключ — короткий id для кнопок, значение — данные голоса.
VOICES = {
    # Русские
    "ru_dmitry":  {"name": "🇷🇺 Дмитрий (муж.)",   "id": "ru-RU-DmitryNeural"},
    "ru_svetlana":{"name": "🇷🇺 Светлана (жен.)",  "id": "ru-RU-SvetlanaNeural"},
    # Английские (US)
    "en_guy":     {"name": "🇺🇸 Guy (муж.)",       "id": "en-US-GuyNeural"},
    "en_aria":    {"name": "🇺🇸 Aria (жен.)",      "id": "en-US-AriaNeural"},
    "en_jenny":   {"name": "🇺🇸 Jenny (жен.)",     "id": "en-US-JennyNeural"},
    "en_chris":   {"name": "🇺🇸 Christopher (муж.)","id": "en-US-ChristopherNeural"},
    # Английские (UK)
    "en_ryan":    {"name": "🇬🇧 Ryan (муж.)",      "id": "en-GB-RyanNeural"},
    "en_sonia":   {"name": "🇬🇧 Sonia (жен.)",     "id": "en-GB-SoniaNeural"},
}

DEFAULT_VOICE = "ru_dmitry"
