"""
Аналитика по своду дел + генерация итогового xlsx-отчёта.

Работает на двух уровнях:
  1. Что было в исходной таблице (даже без скрапинга) — сводки по статусам,
     суммам, инстанциям. Данные в таблице введены вручную, поэтому там
     разнобой ("Апелляция" / "Апелляционная инстанция" / опечатки типа
     "Апклляционнвя инстанция") — здесь это нормализуется по ключевым словам.
  2. Что реально пришло со скрапинга (колонки scraped_*) — если скрапинг
     запускался: доля дел, которые вообще удалось спарсить, актуальный
     статус/дата заседания с самого сайта суда (может отличаться от того,
     что вручную вписано в таблицу — это и есть главная ценность анализа).
"""
from __future__ import annotations

import re

import pandas as pd

RESULT_CANON = {
    "в процессе": ["в процессе", "процесс", "рассматр"],
    "выиграно": ["выиграно", "удовлетвор", "в пользу"],
    "проиграно": ["проиграно", "отказано", "отклон"],
    "мировое соглашение": ["мировое соглашение", "мировое"],
    "закрыто без результата": ["закрыто без результата", "прекращено", "без рассмотрения"],
}

INSTANCE_CANON = {
    "первая инстанция": ["перв"],
    "апелляция": ["апелл"],
    "кассация": ["кассац"],
    "надзор": ["надзор"],
}


def _canon(value: str | None, mapping: dict[str, list[str]]) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or not str(value).strip():
        return "не указано"
    v = str(value).strip().lower()
    for canon, keywords in mapping.items():
        if any(kw in v for kw in keywords):
            return canon
    return f"прочее: {value.strip()}"


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "result" in df.columns:
        df["result_norm"] = df["result"].apply(lambda v: _canon(v, RESULT_CANON))
    if "instance" in df.columns:
        df["instance_norm"] = df["instance"].apply(lambda v: _canon(v, INSTANCE_CANON))
    if "claim_amount" in df.columns:
        df["claim_amount_num"] = pd.to_numeric(df["claim_amount"], errors="coerce")
    return df


def summary_tables(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    df = normalize(df)
    out = {}

    if "court_type" in df.columns:
        out["by_court_type"] = df["court_type"].value_counts().rename_axis("court_type").reset_index(name="count")

    if "result_norm" in df.columns:
        out["by_result"] = df["result_norm"].value_counts().rename_axis("result").reset_index(name="count")

    if "instance_norm" in df.columns:
        out["by_instance"] = df["instance_norm"].value_counts().rename_axis("instance").reset_index(name="count")

    if "party_role" in df.columns:
        out["by_party_role"] = df["party_role"].value_counts().rename_axis("party_role").reset_index(name="count")

    if "claim_amount_num" in df.columns:
        total_claims = df["claim_amount_num"].sum()
        n_with_amount = df["claim_amount_num"].notna().sum()
        out["claims_total"] = pd.DataFrame(
            [{"метрика": "Сумма заявленных требований (сумма по делам с указанной суммой)",
              "значение": total_claims,
              "дел с указанной суммой": n_with_amount}]
        )

    if "scraped_fetched_ok" in df.columns:
        ok = int(df["scraped_fetched_ok"].fillna(False).sum())
        total = int(df["link"].notna().sum())
        out["scrape_coverage"] = pd.DataFrame([{
            "всего ссылок": total,
            "успешно спарсено": ok,
            "не удалось / ошибка": total - ok,
        }])

    if "scraped_error" in df.columns:
        errs = df[df["scraped_error"].notna()][["link", "court_type", "scraped_error"]]
        if not errs.empty:
            out["scrape_errors"] = errs

    if "scraped_next_hearing_date" in df.columns:
        upcoming = df[df["scraped_next_hearing_date"].notna()][
            ["case_number", "court_name", "scraped_next_hearing_date", "link"]
        ].sort_values("scraped_next_hearing_date")
        if not upcoming.empty:
            out["upcoming_hearings"] = upcoming

    return out


def write_report(df: pd.DataFrame, output_path):
    """Пишет полный отчёт в xlsx: лист 'Все дела' (детально) + сводные листы."""
    tables = summary_tables(df)
    df_norm = normalize(df)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_norm.to_excel(writer, sheet_name="Все дела", index=False)
        for name, table in tables.items():
            sheet_name = name[:31]  # ограничение Excel на длину имени листа
            table.to_excel(writer, sheet_name=sheet_name, index=False)

    return output_path


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    p = argparse.ArgumentParser()
    p.add_argument("input_pickle_or_xlsx", type=Path)
    p.add_argument("--output", type=Path, default=Path("data/output/report.xlsx"))
    args = p.parse_args()

    if args.input_pickle_or_xlsx.suffix == ".pkl":
        df = pd.read_pickle(args.input_pickle_or_xlsx)
    else:
        df = pd.read_excel(args.input_pickle_or_xlsx)

    out = write_report(df, args.output)
    print(f"Отчёт сохранён: {out}")
