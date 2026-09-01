"""
Локальный тест парсера против schedule_samples/*.xlsx.
Запуск: python test_parser.py
"""
import glob
import json
import os

from parser import parse_xlsx, flatten_to_days

FILES = [f for f in glob.glob(os.path.join("schedule_samples", "*.xlsx"))
         if not os.path.basename(f).startswith("~$")]


def main():
    if not FILES:
        print("Нет файлов в schedule_samples/")
        return
    total_ok = 0
    for path in FILES:
        print(f"\n=== {os.path.basename(path)} ===")
        with open(path, "rb") as f:
            data = f.read()
        try:
            parsed = parse_xlsx(data)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  !! Ошибка парсинга: {e}")
            continue

        sheets = parsed["sheets"]
        groups = parsed["groups"]
        days = flatten_to_days(parsed)

        print(f"  Листов: {len(sheets)}")
        for s in sheets:
            print(f"    - '{s['name']}': дней={len(s['days'])}")
        print(f"  Всего групп (уникальных): {groups}")
        print(f"  Всего дней в расписании: {len(days)}")

        # Проверка структуры: у каждого дня есть группы, у пары есть num/time/text
        sample_day = None
        max_pairs = 0
        for iso, gmap in sorted(days.items()):
            for gname, lessons in gmap.items():
                max_pairs = max(max_pairs, max((l.get("num") or 0 for l in lessons), default=0))
                for l in lessons:
                    assert l.get("num") is not None, f"нет num в {iso}/{gname}/{l}"
                    assert l.get("text"), f"пустой text в {iso}/{gname}/{l}"
            if sample_day is None and gmap:
                sample_day = (iso, gmap)

        print(f"  Макс. номер пары: {max_pairs}")
        if sample_day:
            iso, gmap = sample_day
            print(f"\n  Пример дня {iso}:")
            for gname, lessons in gmap.items():
                print(f"    [{gname}]")
                for l in lessons:
                    print(f"      пара {l['num']} {l.get('time') or ''}: {l['text']}")

        ok = len(days) > 0 and len(groups) > 0
        total_ok += 1 if ok else 0
        print(f"  -> {'OK' if ok else 'FAIL'}")

    print(f"\nИтого: {total_ok}/{len(FILES)} файлов распознано")


if __name__ == "__main__":
    main()
