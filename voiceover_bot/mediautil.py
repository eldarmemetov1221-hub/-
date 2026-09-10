"""Конвертация аудио через ffmpeg (бинарь берётся из imageio-ffmpeg,
отдельно ffmpeg ставить не надо)."""

import subprocess


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg  # ленивый импорт — нужен только для клонирования
    return imageio_ffmpeg.get_ffmpeg_exe()


def to_wav(src: str, dst: str) -> None:
    """Любой аудиофайл -> моно 22050 Гц wav (формат для XTTS)."""
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-i", src, "-ac", "1", "-ar", "22050", dst],
        check=True,
        capture_output=True,
    )


def to_mp3(src: str, dst: str) -> None:
    """wav -> mp3 (меньше весит для отправки)."""
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-i", src, "-b:a", "192k", dst],
        check=True,
        capture_output=True,
    )
