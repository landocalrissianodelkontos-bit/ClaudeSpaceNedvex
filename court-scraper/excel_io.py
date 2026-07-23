"""
Чтение исходной таблицы дел.

Реальные выгрузки такого рода нередко приходят с "битым" stylesheet.xml
(openpyxl/pandas падают с ValueError на стилях, хотя данные целые).
Поэтому здесь: если обычное чтение не удалось — файл автоматически
пересохраняется через LibreOffice (soffice --headless --convert-to xlsx)
во временную копию и читается уже она.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import openpyxl
import pandas as pd

from config import COLUMN_ALIASES

# Значения OOXML enum'ов, которые реально допускает схема (ECMA-376).
# Некоторые экспортёры (обычно HTML->xlsx конвертеры) пишут в styles.xml
# CSS-значения вроде "-webkit-center" вместо валидного "center" — openpyxl
# на это падает. soffice умеет чинить такое пересохранением, но если
# LibreOffice недоступен (нет дисплея/окружения) или сам файл не открывается
# в headless-режиме, чиним xml руками: заменяем невалидные значения на
# ближайший валидный эквивалент.
_VALID_HORIZONTAL = {"fill", "distributed", "justify", "center", "right", "centerContinuous", "general", "left"}
_VALID_VERTICAL = {"top", "center", "bottom", "justify", "distributed"}
_HORIZONTAL_FALLBACKS = {
    "-webkit-center": "center",
    "-moz-center": "center",
    "-webkit-left": "left",
    "-webkit-right": "right",
}


def _normalize(s: str) -> str:
    return " ".join(str(s).strip().lower().split())


def _build_header_map(header_row: list) -> dict[str, int]:
    """Сопоставляет каноническое имя поля (config.COLUMN_ALIASES) с индексом колонки."""
    norm_headers = {_normalize(h): i for i, h in enumerate(header_row) if h}
    colmap = {}
    for canon, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            alias_n = _normalize(alias)
            for actual, idx in norm_headers.items():
                if alias_n in actual:
                    colmap[canon] = idx
                    break
            if canon in colmap:
                break
    return colmap


def _try_repair_with_soffice(src: Path) -> Path:
    """Пересохраняет xlsx через LibreOffice, возвращает путь к починенной копии."""
    if shutil.which("soffice") is None:
        raise RuntimeError(
            "Файл повреждён (битый stylesheet.xml), а LibreOffice (soffice) не найден "
            "в PATH, чтобы его автоматически починить. Варианты: (1) установите "
            "LibreOffice, (2) откройте файл в Excel/Google Sheets и пересохраните "
            "как .xlsx вручную, затем передайте пересохранённый файл сюда."
        )
    tmp_dir = Path(tempfile.mkdtemp(prefix="xlsx_repair_"))
    cmd = ["soffice", "--headless", "--convert-to", "xlsx", "--outdir", str(tmp_dir), str(src)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    repaired = tmp_dir / src.name
    if not repaired.exists():
        raise RuntimeError(f"Не удалось починить файл через soffice: {src}")
    return repaired


def _fix_enum_attr(xml: str, attr: str, valid: set[str], fallbacks: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        value = m.group(1)
        if value in valid:
            return m.group(0)
        new_value = fallbacks.get(value, next(iter(valid)))
        print(f"[excel_io] styles.xml: {attr}=\"{value}\" невалиден, заменяю на \"{new_value}\"",
              file=sys.stderr)
        return f'{attr}="{new_value}"'

    return re.sub(rf'{attr}="([^"]*)"', repl, xml)


def _try_repair_via_xml_patch(src: Path) -> Path:
    """Чинит битые enum-значения (напр. CSS "-webkit-center" вместо OOXML
    "center") прямо в xl/styles.xml, без внешних зависимостей вроде LibreOffice."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="xlsx_xmlrepair_"))
    repaired = tmp_dir / src.name
    with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(repaired, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/styles.xml":
                text = data.decode("utf-8")
                text = _fix_enum_attr(text, "horizontal", _VALID_HORIZONTAL, _HORIZONTAL_FALLBACKS)
                text = _fix_enum_attr(text, "vertical", _VALID_VERTICAL, {})
                data = text.encode("utf-8")
            zout.writestr(item, data)
    return repaired


def load_workbook_safely(path: Path):
    """Возвращает openpyxl Workbook, при необходимости чиня файл."""
    try:
        return openpyxl.load_workbook(path, data_only=True)
    except Exception as e:
        print(f"[excel_io] Обычное чтение не удалось ({e!r}), пробую починить XML напрямую...",
              file=sys.stderr)
        try:
            repaired = _try_repair_via_xml_patch(path)
            return openpyxl.load_workbook(repaired, data_only=True)
        except Exception as xml_e:
            print(f"[excel_io] Починка XML не помогла ({xml_e!r}), пробую через soffice...",
                  file=sys.stderr)
            repaired = _try_repair_with_soffice(path)
            return openpyxl.load_workbook(repaired, data_only=True)


def read_cases(path: Path, sheets: list[str] | None = None) -> pd.DataFrame:
    """
    Читает все листы таблицы и возвращает единый DataFrame с каноническими
    именами колонок (см. config.COLUMN_ALIASES) + служебной колонкой sheet_name.
    Строки без ссылки и без номера дела отбрасываются.
    """
    wb = load_workbook_safely(path)
    target_sheets = sheets or wb.sheetnames

    frames = []
    for sn in target_sheets:
        if sn not in wb.sheetnames:
            print(f"[excel_io] Лист '{sn}' не найден, пропускаю", file=sys.stderr)
            continue
        ws = wb[sn]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header, *data_rows = rows
        colmap = _build_header_map(list(header))
        if "link" not in colmap:
            print(f"[excel_io] На листе '{sn}' не нашёл колонку со ссылкой, пропускаю", file=sys.stderr)
            continue

        records = []
        for row in data_rows:
            if row is None or all(v is None for v in row):
                continue
            rec = {"sheet_name": sn}
            for canon, idx in colmap.items():
                rec[canon] = row[idx] if idx < len(row) else None
            records.append(rec)

        df = pd.DataFrame(records)
        frames.append(df)

    if not frames:
        raise RuntimeError("Не удалось прочитать ни одного листа с данными")

    result = pd.concat(frames, ignore_index=True)
    # Отбрасываем строки совсем без ссылки и без номера дела — это, как правило,
    # либо пустые "хвосты" таблицы, либо разделители.
    result = result[~(result.get("link").isna() & result.get("case_number").isna())]
    result = result.reset_index(drop=True)
    result["row_id"] = result.index
    return result


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("input", type=Path)
    args = p.parse_args()

    df = read_cases(args.input)
    print(f"Прочитано строк: {len(df)}")
    print(df[["row_id", "sheet_name", "case_number", "court_name", "link"]].head(10).to_string())
