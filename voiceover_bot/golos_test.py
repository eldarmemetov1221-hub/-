"""Прослушать РАЗНЫЕ голоса Яндекса и выбрать самый живой.

Тестирует ДВЕ версии SpeechKit:
  • v1 (старая, что у нас сейчас) — filipp, jane, alena, ermil;
  • v3 (новая, голоса как на сайте Яндекса — живые, чёткие) — marina, alexander,
    kirill, anton, jane и др., с «ролями» (friendly/good/neutral).

Для v3 нужен пакет speechkit (ставится один раз):
    pip install speechkit

Запуск (ключи как для билетов):
    python golos_test.py

Потом открой папку golosa/ и послушай. Файлы v3 — с приставкой v3_.
"""

import os
import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

PHRASE = ("Внимание, вопрос! Кому вы должны уступить дорогу при повороте направо? "
          "У тебя пять секунд, время пошло!")

# v1 — старая версия (что сейчас)
V1_VOICES = ["filipp", "jane", "alena", "ermil"]

# v3 — новая версия, живые голоса (голос, роль)
V3_VOICES = [
    ("marina", "friendly"),
    ("alexander", "neutral"),
    ("kirill", "neutral"),
    ("anton", "good"),
    ("jane", "good"),
    ("ermil", "good"),
    ("dasha", "friendly"),
    ("julia", "strict"),
]


def _save(data_bytes_or_segment, path, is_segment):
    if is_segment:
        data_bytes_or_segment.export(str(path), format="mp3")
    else:
        ogg = Path(str(path) + ".ogg")
        ogg.write_bytes(data_bytes_or_segment)
        subprocess.run([FFMPEG, "-y", "-i", str(ogg), str(path)], capture_output=True)
        ogg.unlink(missing_ok=True)


def main() -> None:
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if not key or not folder:
        raise SystemExit("Задай YANDEX_API_KEY и YANDEX_FOLDER_ID (как для билетов).")

    out = Path("golosa")
    out.mkdir(exist_ok=True)

    # --- v1 (старые) ---
    os.environ.setdefault("YANDEX_EMOTION", "good")
    os.environ.setdefault("YANDEX_SPEED", "1.0")
    os.environ.setdefault("YANDEX_VOLUME", "1.0")
    os.environ.setdefault("YANDEX_PITCH", "1.0")
    from auto_quiz import synth_yandex
    print("── v1 (старые голоса) ──")
    for v in V1_VOICES:
        try:
            _save(synth_yandex(PHRASE, v, key, folder), out / f"v1_{v}.mp3", False)
            print(f"  ✅ v1_{v}.mp3")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ v1_{v}: {e}")

    # --- v3 (новые, живые) ---
    print("\n── v3 (новые живые голоса, как на сайте Яндекса) ──")
    # pydub (его тянет yandex-speechkit) должен знать путь к ffmpeg.
    try:
        import pydub
        pydub.AudioSegment.converter = FFMPEG
        pydub.AudioSegment.ffmpeg = FFMPEG
        pydub.AudioSegment.ffprobe = FFMPEG
    except Exception:  # noqa: BLE001
        pass
    try:
        from speechkit import model_repository, configure_credentials, creds
    except ImportError as e:
        print(f"  ⚠️ Не удалось импортировать v3: {e}")
        print("     Поставь официальный пакет и свежий protobuf:")
        print("       pip install yandex-speechkit \"protobuf>=3.20,<5\"")
        print("\nПока открой папку golosa и послушай v1_*.")
        return

    configure_credentials(yandex_credentials=creds.YandexCredentials(api_key=key))
    for voice, role in V3_VOICES:
        try:
            model = model_repository.synthesis_model()
            model.voice = voice
            model.role = role
            seg = model.synthesize(PHRASE, raw_format=False)
            _save(seg, out / f"v3_{voice}_{role}.mp3", True)
            print(f"  ✅ v3_{voice}_{role}.mp3")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ v3_{voice}_{role}: {e}")

    print("\nГотово. Открой папку 'golosa' и послушай. v3_* — живые голоса. "
          "Скажи, какой заходит — поставлю на тик-токи.")


if __name__ == "__main__":
    main()
