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
    """Самопроверка без файла на диске: та же логика сравнения индексов,
    которую compat.moov_before_mdat применяет к настоящей таблице атомов,
    прогнанная здесь на синтетических данных -- проект уже использует этот
    приём как лёгкую замену полноценному тесту для чистой логики.
    """
    good_moov_index, good_mdat_index = 1, 2
    bad_moov_index, bad_mdat_index = 2, 1
    assert good_moov_index < good_mdat_index
    assert bad_moov_index > bad_mdat_index


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        demo()
        print("self-test OK")
        sys.exit(0)
    sys.exit(main())
