"""
Гибкий парсер расписания из .xlsx.

Считывает файл напрямую из XML-структуры (zip + ElementTree) средствами
только стандартной библиотеки Python -- поэтому работает идентично и в
локальной среде для тестов, и в браузере (Pyodide/WASM) в офлайне.

Дизайн-принципы:
  * Устойчивость: не полагаемся на openpyxl (некоторые файлы падают на
    невалидных стилях рамок). Читаем sharedStrings + листы сами.
  * Гибкость: колонки определяем ПО ИМЕНИ / смыслу (синонимы + регулрки),
    а не по фиксированным координатам. Число групп и число пар вычисляем
    динамически. Обрабатываем все листы; при совпадении дат поздний лист
    (больший индекс) перезаписывает ранний.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# ---------------------------------------------------------------------------
# Утилиты нормализации текста
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Приводим к нижнему регистру, вакуум: лишние пробелы/дефисы и всякие symbol."""
    if text is None:
        return ""
    text = str(text)
    # неразрывные пробелы и тире -> обычные
    text = text.replace("\u00a0", " ").replace("\u2009", " ")
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2012", "-")
    text = text.replace("ё", "е")
    # собираем слова
    words = re.findall(r"[а-яёa-z0-9]+", text.lower())
    return " ".join(words)


def _remove_diacritics_hint(text: str) -> str:
    return text

# ---------------------------------------------------------------------------
# Загрузка документа
# ---------------------------------------------------------------------------

def _column_index(ref: str) -> int:
    i = 0
    for ch in ref:
        if ch.isalpha():
            i = i * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return i - 1


def _row_index(ref: str) -> int:
    m = re.search(r"(\d+)$", ref)
    return int(m.group(1)) - 1 if m else -1


def _parse_range(rng: str) -> Tuple[int, int, int, int]:
    """'A4:A11' -> (r0, c0, r1, c1) 0-based inclusive."""
    a, _, b = rng.partition(":")
    if not b:
        b = a
    r0, c0 = _row_index(a), _column_index(a)
    r1, c1 = _row_index(b), _column_index(b)
    return r0, c0, r1, c1


def _excel_serial_to_date(serial: float) -> Optional[date]:
    # Excel эпоха: историческая ошибка 1900 leap. Дни считаем от 1899-12-30.
    try:
        return date(1899, 12, 30) + timedelta(days=int(float(serial)))
    except (TypeError, ValueError):
        return None


def _read_shared_strings(zf: zipfile.ZipFile, path: str) -> List[str]:
    xml = zf.read(path).decode("utf-8", errors="replace")
    root = ET.fromstring(xml)
    strings: List[str] = []
    for si in root.findall(f"{NS}si"):
        text = "".join(si.itertext())
        strings.append(text.replace("\u00a0", " ").strip())
    return strings


def _sheet_index_from_name(name: str) -> int:
    """Из 'xl/worksheets/sheet5.xml' достаём 5 для сортировки."""
    m = re.search(r"sheet(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _resolve_parts(zf: zipfile.ZipFile) -> Tuple[List[str], List[str]]:
    """
    Достаёт имена листов (по порядку) и их пути в архиве.
    Возвращает (sheet_names, sheet_paths). Пути разрешаются через
    xl/_rels/workbook.xml.rels по типу relationship -> worksheet.
    """
    # relId -> (target, type)
    rels_path = "xl/_rels/workbook.xml.rels"
    rel_relid_to_target: Dict[str, str] = {}
    rel_relid_to_type: Dict[str, str] = {}
    try:
        rel_xml = zf.read(rels_path).decode("utf-8", errors="replace")
        rel_root = ET.fromstring(rel_xml)
        for rel in rel_root:
            rid = rel.get("Id")
            target = rel.get("Target")
            rtype = rel.get("Type", "")
            if rid and target:
                rel_relid_to_target[rid] = target
                rel_relid_to_type[rid] = rtype
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        rel_relid_to_target, rel_relid_to_type = {}, {}

    # предварительный список листов из workbook.xml (rId каждого листа)
    sheet_rows: List[Tuple[str, str, str]] = []  # (name, rid, target)
    try:
        wb_xml = zf.read("xl/workbook.xml").decode("utf-8", errors="replace")
        wb_root = ET.fromstring(wb_xml)
        for sh in wb_root.iter(f"{NS}sheet"):
            name = sh.get("name", "")
            rid = sh.get(f"{REL_NS}id") or ""
            sheet_rows.append((name, rid, rel_relid_to_target.get(rid, "")))
    except (KeyError, ET.ParseError):
        pass

    sheet_names: List[str] = []
    sheet_paths: List[str] = []
    for name, rid, target in sheet_rows:
        sheet_names.append(name)
        if target:
            sheet_paths.append(_sheet_path(target))
        else:
            # резерв: пытаемся взять любой worksheet-relationship по порядку
            sheet_paths.append("")

    # Если рИд не сопоставился ни с одним worksheet, но есть worksheet-реляции,
    # заполняем порядковые пути
    ws_targets = [rel_relid_to_target[k] for k in rel_relid_to_target
                  if "worksheet" in rel_relid_to_type.get(k, "")]
    if sheet_paths and not any(sheet_paths):
        sheet_paths = [_sheet_path(t) for t in ws_targets]

    return sheet_names, sheet_paths


def _sheet_path(rel_target: str) -> str:
    """Преобразует относительный target (worksheets/sheet1.xml) к полному пути."""
    if rel_target.startswith("/"):
        return rel_target.lstrip("/")
    base = "xl"
    # если target начинается с 'xl/' - он уже абсолютный относительно архива
    if rel_target.startswith("xl/"):
        return rel_target
    if rel_target.startswith("../"):
        # поднятие из xl/: xl/_rels/../../ -> проект корня
        parts = rel_target.split("/")
        up = 0
        path_parts = []
        for p in parts:
            if p == "..":
                up += 1
            else:
                path_parts.append(p)
        folder = "/".join(path_parts)
        if up == 1:  # из xl/_rels/ поднимаемся до xl/
            return f"xl/{folder}"
        return folder
    return f"{base}/{rel_target}" if rel_target else ""


def _parse_sheet(zf: zipfile.ZipFile, sheet_path: str, shared: List[str]) -> List[Dict[str, Optional[str]]]:
    xml = zf.read(sheet_path).decode("utf-8", errors="replace")
    root = ET.fromstring(xml)

    sheetdata = root.find(f"{NS}sheetData")
    if sheetdata is None:
        return []

    # Карта : r1c1-адреса объединённых ячеек -> значение в начальной ячейке (top-left)
    merged_value: Dict[Tuple[int, int], Optional[str]] = {}

    rows: List[Dict[int, Optional[str]]] = []  # row_index -> {col_index: value}
    max_row = 0

    for row_el in sheetdata.findall(f"{NS}row"):
        r = int(row_el.get("r") or 1) - 1
        while len(rows) <= r:
            rows.append({})
        row_map = rows[r]
        for c in row_el.findall(f"{NS}c"):
            ref = c.get("r", "")
            ci = _column_index(ref)
            t = c.get("t", "")
            value: Optional[str] = None

            if t == "s":
                v_el = c.find(f"{NS}v")
                if v_el is not None and v_el.text is not None:
                    try:
                        value = shared[int(v_el.text)]
                    except (ValueError, IndexError):
                        value = None
            elif t == "inlineStr":
                is_el = c.find(f"{NS}is")
                if is_el is not None:
                    value = "".join(is_el.itertext())
            else:
                v_el = c.find(f"{NS}v")
                if v_el is not None and v_el.text is not None:
                    value = v_el.text.strip()

            if value is not None and value != "":
                row_map[ci] = value
            # сохраняем как есть даже пустые? нет, только значащие, т.к. пустые нам не нужны

        max_row = max(max_row, r)

    # Объединённые ячейки.
    # Заполняем объединения, которые охватывают НЕСКОЛЬКО КОЛОНОК (горизонтальные),
    # т.е. общее занятие на обе группы. Вертикальные объединения (например дата
    # в колонке А на весь день) не размножаем -- иначе каждая строка блока стала
    # бы выглядеть как новая дата.
    for mc in root.iter(f"{NS}mergeCell"):
        rng = mc.get("ref")
        if not rng:
            continue
        r0, c0, r1, c1 = _parse_range(rng)
        # пропускаем чисто вертикальные (одна колонка, несколько строк)
        if c0 == c1 and r0 != r1:
            continue
        # собираем значение из первой непустой ячейки в диапазоне
        val: Optional[str] = None
        for rr in range(r0, r1 + 1):
            for cc in range(c0, c1 + 1):
                if len(rows) > rr:
                    got = rows[rr].get(cc)
                    if got:
                        val = got
                        break
            if val:
                break
        for rr in range(r0, r1 + 1):
            for cc in range(c0, c1 + 1):
                while len(rows) <= rr:
                    rows.append({})
                # раздаём значение объединения всем ячейкам диапазона
                rows[rr].setdefault(cc, "" if val is None else val)

    # Приводим к списку словарей по привычным именам колонок (A, B, C, ...) для удобства
    result: List[Dict[str, Optional[str]]] = []
    for r in range(len(rows)):
        d: Dict[str, Optional[str]] = {}
        for ci, v in rows[r].items():
            if v:
                col_name = _col_letter(ci)
                d[col_name] = v
        # если строка пустая, оставляем {} -- пустые строки парсер пропустит
        result.append(d)
    return result


def _col_letter(ci: int) -> str:
    s = ""
    ci += 1
    while ci:
        ci, rem = divmod(ci - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


# ---------------------------------------------------------------------------
# Определение колонок по именам (гибко)
# ---------------------------------------------------------------------------

def _looks_like_date_header(t: str) -> bool:
    return _norm(t) in ("день", "дата", "число", "день недели", "дата занятия")


def _looks_like_lesson_header(t: str) -> bool:
    n = _norm(t)
    return n in ("пара", "пары", "занятие", "время", "№ пары", "пара (время)", "час") or \
        re.fullmatch(r"(пара|заняти[ея]|время|час)[\s]*(\(.*\))?", n) is not None


def _looks_like_group_label(t: str) -> bool:
    n = _norm(t)
    return "группа" in n or "групп" in n or "подгрупп" in n or n.startswith("групп")


def _looks_like_group_code(t: str) -> bool:
    # код типа "04.ЛОБ.24.ЦЛ(АЯиКЯ).1" или аббревиатура с точками/№
    t2 = (t or "").strip()
    if not t2:
        return False
    if "группа" in t2.lower() or "групп" in t2.lower():
        return True
    return bool(re.search(r"\d{2,3}\.[\w\.]+\d", t2)) or bool(re.fullmatch(r"[№\d\.]+[^\s]*", t2))


def _looks_like_lesson_cell(t: str) -> bool:
    # "1 пара 08.30-10.00"
    n = _norm(t)
    return bool(re.match(r"^\d+\s+(пара|занятие|час)", n)) and \
        bool(re.search(r"\d{2}[:.]\d{2}", t))


# ---------------------------------------------------------------------------
# Структура дня
# ---------------------------------------------------------------------------

def _find_block_boundaries(rows: List[Dict[str, Optional[str]]], date_col: str):
    """
    Возвращает список индексов строк, где начинается новый день-блок:
    в этих строках в колонке-дате есть читаемая дата.
    """
    boundaries: List[int] = []
    for i, row in enumerate(rows):
        v = row.get(date_col)
        if _date_from_value(v) is not None:
            boundaries.append(i)
    return boundaries


_DATE_PATTERNS = [
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),                       # 2026-09-01
    re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$"),     # 1.9.2026 / 01.09.26
]


def _date_from_value(v) -> Optional[date]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Excel serial
    try:
        fv = float(s)
        if 1 < fv < 70000 and re.fullmatch(r"\d+(\.\d+)?", s):
            return _excel_serial_to_date(fv)
    except (ValueError, TypeError):
        pass
    for pat in _DATE_PATTERNS:
        m = pat.match(s)
        if m:
            if "-" in s:
                try:
                    return date.fromisoformat(s)
                except ValueError:
                    return None
    m = _DATE_PATTERNS[1].match(s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    # длинное название месяца, напр. "1 сентября 2026"
    m = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", s.lower())
    if m:
        months = {"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
                  "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}
        mon = months.get(m.group(2))
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                return None
    return None


def _lesson_info(cell: str) -> Tuple[Optional[int], Optional[str]]:
    """Из '3 пара 12.05-13.35' достаём (номер, время '12:05-13:35')."""
    if not cell:
        return None, None
    m = re.match(r"^\s*(\d+)\s*(пара|занятие|час)?", cell, re.I)
    num = int(m.group(1)) if m else None
    tm = None
    t = re.search(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})", cell)
    if t:
        tm = f"{t.group(1).zfill(2)}:{t.group(2)}-{t.group(3).zfill(2)}:{t.group(4)}"
    return num, tm


# ---------------------------------------------------------------------------
# Главный алгоритм
# ---------------------------------------------------------------------------

def _guess_columns(rows: List[Dict[str, Optional[str]]]) -> Dict[str, object]:
    """
    Ищем строки-заголовки и определяем колонки: дата, пара, и список групп.
    Возвращает {'date': col, 'lesson': col, 'groups': [(col, display_name)],
                'header_row': int}.
    Группы определяются динамически по всем колонкам правее колонки 'пара',
    у которых есть непустое значение в зоне заголовков (строка с шапкой и
    строка-две ниже). Отображаемое имя группы: предпочитаем название с
    'группа/групп'; иначе код из строки-шапки; иначе буква колонки.
    """
    for hi, row in enumerate(rows[:60]):
        dcol = None
        lcol = None
        for col, val in row.items():
            if val is None:
                continue
            if _looks_like_date_header(val):
                dcol = col
            elif _looks_like_lesson_header(val):
                lcol = col
        if dcol and lcol:
            return _collect_groups(rows, hi, dcol, lcol)
    return {"date": None, "lesson": None, "groups": [], "header_row": None}


def _collect_groups(rows: List[Dict[str, Optional[str]]], hi: int,
                    dcol: str, lcol: str) -> Dict[str, object]:
    """hi -- индекс строки-шапки с датой и парой."""
    # зона заголовков: шапка + до 3 строк ниже (имена групп обычно в строке ниже)
    zone = range(hi, min(len(rows), hi + 4))
    lcol_idx = _column_index(lcol)

    # собираем для каждой колонки все значения в зоне (кроме колонок даты/пары)
    col_values: Dict[str, List[str]] = {}
    for r in zone:
        for col, val in rows[r].items():
            if val is None:
                continue
            if col in (dcol, lcol):
                continue
            ci = _column_index(col)
            if ci <= lcol_idx:
                continue
            if _looks_like_date_header(val) or _looks_like_lesson_header(val):
                continue
            col_values.setdefault(col, []).append(str(val))

    group_defs: List[Tuple[str, str]] = []
    for col, vals in col_values.items():
        cleaned = [v.strip() for v in vals if v and str(v).strip()]
        if not cleaned:
            continue
        name = _pick_group_name(col, cleaned)
        group_defs.append((col, name))

    group_defs.sort(key=lambda x: _column_index(x[0]))
    return {"date": dcol, "lesson": lcol, "groups": group_defs, "header_row": hi}


def _pick_group_name(col: str, values: List[str]) -> str:
    # 1) значение с явным "группа"
    for v in values:
        if _looks_like_group_label(v):
            return v
    # 2) значение-код (то что не является только буквой-заголовком)
    for v in values:
        if _looks_like_group_code(v) and not _looks_like_date_header(v):
            return v
    # 3) короткая подпись типа "1 группа" уже отловлена выше; fallback
    for v in values:
        return v
    return f"Группа {_col_letter(_column_index(col))}"


def parse_xlsx(data: bytes) -> Dict:
    """
    Принимает байты .xlsx, возвращает нормализованное расписание:
      {
        "sheets": [ {"name":..., "days": [{"date": "2026-09-01",
                                             "groups": {"1 ЯЗЫКОВАЯ ГРУППА": [ {num,time,text}, ... ]}}]} ],
        "groups": [...]   # уникальные имена групп по всем листам
      }
    """
    zf = zipfile.ZipFile(io.BytesIO(data))

    # sharedStrings (путь может варьироваться)
    shared: List[str] = []
    for name in zf.namelist():
        if name.endswith("sharedStrings.xml") or name.endswith("/sharedStrings.xml"):
            shared = _read_shared_strings(zf, name)
            break

    sheet_names, sheet_paths = _resolve_parts(zf)

    all_sheets: List[Dict] = []
    all_groups: List[str] = []
    seen_group: set = set()

    for idx, (sname, path) in enumerate(zip(sheet_names, sheet_paths)):
        if not path or path not in zf.namelist():
            # ищем лист по индексу среди worksheet-файлов
            ws_files = sorted(
                [n for n in zf.namelist() if "/worksheets/" in n and n.endswith(".xml")],
                key=lambda n: _sheet_index_from_name(n),
            )
            if idx < len(ws_files):
                path = ws_files[idx]
            else:
                continue

        try:
            rows = _parse_sheet(zf, path, shared)
        except Exception:
            continue

        cols = _guess_columns(rows)
        date_col, lesson_col = cols.get("date"), cols.get("lesson")
        group_defs = cols.get("groups", [])

        if not date_col or not lesson_col:
            continue

        days: List[Dict] = []
        boundaries = _find_block_boundaries(rows, date_col)

        # разбиваем на блоки по дням
        for bi, start in enumerate(boundaries):
            end = boundaries[bi + 1] if bi + 1 < len(boundaries) else len(rows)
            block_rows = rows[start:end]

            dval = None
            for row in block_rows[:3]:
                dval = row.get(date_col)
                if _date_from_value(dval) is not None:
                    break
            day_date = _date_from_value(dval)
            if day_date is None:
                continue
            iso = day_date.isoformat()

            # парные записи группы -> {lesson_num: {num,time,text}}
            group_map: Dict[str, List[Dict]] = {}

            def _ensure(gname: str):
                if gname not in group_map:
                    group_map[gname] = []

            # инициализируем все группы
            group_by_col = {}
            for col, gname_resolved in group_defs:
                gname = gname_resolved or f"Группа {_col_letter(_column_index(col))}"
                group_by_col[col] = gname
                _ensure(gname)

            # проходим по строкам блока
            for row in block_rows:
                lesson_cell = row.get(lesson_col)
                # определяем, есть ли в этой строке занятие (номер пары)
                num, tm = _lesson_info(lesson_cell or "")
                if num is None:
                    continue

                # для каждой группы-колонки смотрим текст занятия
                any_lesson = False
                for col, gname in group_by_col.items():
                    text_vals = []
                    val = row.get(col)
                    if val and _clean_lesson(val):
                        text_vals.append(_clean_lesson(val))
                    if text_vals:
                        any_lesson = True
                        _ensure(gname)
                        for txt in text_vals:
                            group_map[gname].append({
                                "num": num,
                                "time": tm,
                                "text": txt,
                            })
                    # если у этой пары нет занятия ни у каких групп, всё равно добавим
                # если строка вообще без занятий - пропускаем (нет смысла)

            # сжимаем: если у пары занятие общее для всех групп (по merged C:D),
            # мы это уже отловили через чтение merged-ячеек (значения раздают всем).
            # Удаляем паразитные пустые/дубликаты одинаковые подряд не делаем.
            final_groups = {}
            for gname, lessons in group_map.items():
                # дедупликация смежных одинаковых текстов для одной пары -- оставим как есть,
                # но уберём полностью пустые записи
                final_groups[gname] = lessons

            if final_groups:
                days.append({"date": iso, "groups": final_groups})

        if days:
            all_sheets.append({"name": sname, "days": days})
            for g in group_by_col.values():
                if g not in seen_group:
                    seen_group.add(g)
                    all_groups.append(g)

    return {"sheets": all_sheets, "groups": all_groups}


_LESSON_CLEAN = re.compile(r"\s+")

def _clean_lesson(t: str) -> Optional[str]:
    if not t:
        return None
    t = str(t).strip().replace("\u00a0", " ")
    t = _LESSON_CLEAN.sub(" ", t).strip()
    return t if t else None


# ---------------------------------------------------------------------------
# Слияние (качество данных). Вызывается на фронте, здесь -- как функция,
# чтобы её можно было и локально протестировать.
# ---------------------------------------------------------------------------

def merge_into(existing: Dict, new: Dict) -> Dict:
    """
    existing/new: {"2026-09-01": {"Группа 1": [lessons...]}}
    Новые даты перезаписывают пересечения; отсутствующие -- сохраняются.
    Возвращает обновлённый словарь.
    """
    out = dict(existing)
    for iso, groups in (new or {}).items():
        out[iso] = groups
    return out


def flatten_to_days(parsed: Dict) -> Dict[str, Dict[str, list]]:
    """
    Из результата parse_xlsx строим единую карту дата -> {группа: [занятия]},
    объединяя все листы. Поздний лист перезаписывает ранний по датам.
    """
    merged: Dict[str, Dict[str, list]] = {}
    for sheet in parsed.get("sheets", []):
        for day in sheet.get("days", []):
            iso = day["date"]
            # перезаписываем целиком день более свежим листом
            merged[iso] = merge_into(merged.get(iso, {}), day["groups"])
    return merged


def parse_to_json(data: bytes) -> str:
    """
    Основная точка входа для фронтенда: принимает байты .xlsx,
    возвращает JSON-строку вида {"groups": [...], "days": {date: {group: [lessons]}}}.
    """
    import json
    parsed = parse_xlsx(data)
    days = flatten_to_days(parsed)
    return json.dumps({"groups": parsed.get("groups", []), "days": days},
                      ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI для локальных тестов
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    def _dump(path: str):
        with open(path, "rb") as f:
            data = f.read()
        parsed = parse_xlsx(data)
        days = flatten_to_days(parsed)
        out = {
            "sheets": [
                {"name": s["name"], "dates": [d["date"] for d in s["days"]]}
                for s in parsed["sheets"]
            ],
            "groups": parsed["groups"],
            "day_count": len(days),
            "days": days,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])

    for p in sys.argv[1:]:
        _dump(p)
