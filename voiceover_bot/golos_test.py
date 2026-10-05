"""Быстро озвучить одну фразу РАЗНЫМИ голосами Яндекса (с эмоцией «радостный»),
чтобы выбрать самый живой. Сохраняет mp3 в папку golosa/.

Запуск (ключи Яндекса должны быть в YANDEX_API_KEY / YANDEX_FOLDER_ID):
    python golos_test.py

Потом открой папку golosa/ и послушай файлы — скажи, какой голос нравится.
"""

import os
import subprocess
from pathlib import Path

import imageio_ffmpeg
from auto_quiz import synth_yandex

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

PHRASE = ("Внимание, вопрос! Кому вы должны уступить дорогу при повороте направо? "
          "У тебя пять секунд. Поехали!")

# Голоса Яндекса, которые звучат живее остальных.
VOICES = ["jane", "alena", "ermil", "filipp", "omazh", "zahar", "marina", "alexander"]


def main() -> None:
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if not key or not folder:
        raise SystemExit("Задай YANDEX_API_KEY и YANDEX_FOLDER_ID (как для билетов).")

    os.environ.setdefault("YANDEX_EMOTION", "good")   # радостный = живее
    os.environ.setdefault("YANDEX_SPEED", "1.0")
    os.environ.setdefault("YANDEX_VOLUME", "1.0")
    os.environ.setdefault("YANDEX_PITCH", "1.0")

    out = Path("golosa")
    out.mkdir(exist_ok=True)
    for v in VOICES:
        try:
            data = synth_yandex(PHRASE, v, key, folder)
            ogg = out / f"{v}.ogg"
            ogg.write_bytes(data)
            mp3 = out / f"{v}.mp3"
            subprocess.run([FFMPEG, "-y", "-i", str(ogg), str(mp3)],
                           capture_output=True)
            ogg.unlink(missing_ok=True)
            print(f"  ✅ {v}  ->  golosa/{v}.mp3")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {v}: {e}")
    print("\nГотово. Открой папку 'golosa' и послушай — скажи, какой голос заходит.")


if __name__ == "__main__":
    main()
