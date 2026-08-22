#!/usr/bin/env python3
"""Машинная проверка faststart: moov обязан идти раньше mdat в MP4.

Без +faststart moov лежит в конце файла, и плеер выкачивает весь файл до
первого кадра.

Тонкая обёртка: сама логика (`orchestrator.media.compat.moov_before_mdat`)
теперь живёт там, потому что она нужна и внутри работающего контейнера
(D-44 — решение "уже готов, ffmpeg не запускаем" принимается на рантайме),
а не только здесь, при разработке. Два места, одна логика — дублировать её
было бы граблей.

Этот скрипт при этом обязан продолжать работать под python хоста, где нет
виртуального окружения проекта и `orchestrator` не установлен как пакет --
`tools/make_control_clip.sh` и `tools/make_error_clips.sh` вызывают его
именно так. Поэтому перед импортом корень репозитория подставляется в
sys.path вручную, а `orchestrator.media.compat` намеренно не тянет ничего
стороннего на уровне модуля (см. её докстринг).

Usage: check_faststart.py <mp4_path>
Exit code: 0 если moov раньше mdat, 1 иначе (или при ошибке чтения).
"""
import sys
from pathlib import Path

# Хостовый python не видит пакет orchestrator (он ставится editable только
# внутри venv/образа) -- подставляем корень репозитория в sys.path перед
# импортом, а не полагаемся на PYTHONPATH или установку пакета.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.media.compat import moov_before_mdat  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("usage: check_faststart.py <mp4_path>", file=sys.stderr)
        return 1

    path = sys.argv[1]
    try:
        ok = moov_before_mdat(path)
    except OSError as exc:
        print(f"ERROR: не удалось прочитать '{path}': {exc}", file=sys.stderr)
        return 1

    if not ok:
        print(
            "ERROR: moov не идёт раньше mdat (или один из них отсутствует в "
            "top-level атомах) — файл не faststart. Плеер выкачает весь файл "
            "до первого кадра. Пересоберите с -movflags +faststart.",
            file=sys.stderr,
        )
        return 1

    print("OK: moov раньше mdat (faststart подтверждён)")
    return 0


def demo():
    """Self-check against the real `compat.moov_before_mdat`, on two
    minimal atom tables this function writes itself -- one faststart, one
    not.

    It used to compare two hardcoded integers (`assert 1 < 2`) and import
    nothing from the module it claimed to be checking. An assertion that
    cannot fail is worse than no assertion, because it reads as coverage.
    """
    import struct
    import tempfile

    def atom_file(order):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            for atom_type in order:
                # An 8-byte header declaring an 8-byte box: header only,
                # no payload. read_top_level_atoms needs nothing more.
                f.write(struct.pack(">I4s", 8, atom_type))
            return Path(f.name)

    faststart = atom_file([b"ftyp", b"moov", b"mdat"])
    not_faststart = atom_file([b"ftyp", b"mdat", b"moov"])
    no_moov = atom_file([b"ftyp", b"mdat"])
    try:
        assert moov_before_mdat(faststart)
        assert not moov_before_mdat(not_faststart)
        assert not moov_before_mdat(no_moov)
    finally:
        for path in (faststart, not_faststart, no_moov):
            path.unlink()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        demo()
        print("self-test OK")
        sys.exit(0)
    sys.exit(main())
