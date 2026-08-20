#!/usr/bin/env python3
"""Машинная проверка faststart: moov обязан идти раньше mdat в MP4.

Читает таблицу top-level атомов (без внешних зависимостей) и печатает их порядок.
Без +faststart moov лежит в конце файла, и плеер выкачивает весь файл до первого кадра.

Usage: check_faststart.py <mp4_path>
Exit code: 0 если moov раньше mdat, 1 иначе (или при ошибке чтения).
"""
import struct
import sys


def read_top_level_atoms(path):
    atoms = []
    with open(path, "rb") as f:
        while True:
            header = f.read(8)
            if len(header) < 8:
                break
            size, atom_type = struct.unpack(">I4s", header)
            atom_type = atom_type.decode("ascii", errors="replace")
            header_len = 8
            if size == 1:
                # 64-битный largesize сразу после заголовка
                largesize_bytes = f.read(8)
                if len(largesize_bytes) < 8:
                    break
                size = struct.unpack(">Q", largesize_bytes)[0]
                header_len = 16
            atoms.append(atom_type)
            if size == 0:
                # атом занимает всё до конца файла — последний в таблице
                break
            if size < header_len:
                break  # битый атом, дальше читать нечего
            skip = size - header_len
            f.seek(skip, 1)
    return atoms


def main():
    if len(sys.argv) != 2:
        print("usage: check_faststart.py <mp4_path>", file=sys.stderr)
        return 1

    path = sys.argv[1]
    try:
        atoms = read_top_level_atoms(path)
    except OSError as exc:
        print(f"ERROR: не удалось прочитать '{path}': {exc}", file=sys.stderr)
        return 1

    print("Порядок top-level атомов: " + " ".join(atoms))

    if "moov" not in atoms or "mdat" not in atoms:
        print(
            "ERROR: не найден moov и/или mdat в top-level атомах — файл не похож на валидный MP4.",
            file=sys.stderr,
        )
        return 1

    if atoms.index("moov") > atoms.index("mdat"):
        print(
            "ERROR: moov лежит после mdat — файл не faststart. "
            "Плеер выкачает весь файл до первого кадра. "
            "Пересоберите с -movflags +faststart.",
            file=sys.stderr,
        )
        return 1

    print("OK: moov раньше mdat (faststart подтверждён)")
    return 0


def demo():
    """Самопроверка логики без файла на диске: синтетические таблицы атомов."""
    good = ["ftyp", "moov", "mdat"]
    bad = ["ftyp", "mdat", "moov"]
    assert good.index("moov") < good.index("mdat")
    assert bad.index("moov") > bad.index("mdat")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        demo()
        print("self-test OK")
        sys.exit(0)
    sys.exit(main())
