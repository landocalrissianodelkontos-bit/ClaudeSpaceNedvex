"""
CLI для парсера судебных дел.

Примеры:
    # 1. Только посмотреть, как распределились ссылки по типам судов
    #    (сеть не нужна, полезно сразу после получения новой таблицы)
    python main.py classify --input input.xlsx

    # 2. Смок-тест: спарсить по 2 дела с каждого типа сайта, посмотреть на
    #    результат до того, как гонять всю таблицу
    python main.py scrape --input input.xlsx --limit 2

    # 3. Полный прогон: скрапинг всех дел + сохранение результатов
    python main.py scrape --input input.xlsx

    # 4. Отчёт: собрать xlsx с сводками (использует то, что уже накопилось
    #    в кэше data/raw после шагов 2-3; сеть не нужна)
    python main.py analyze --input input.xlsx --output data/output/report.xlsx

    # 5. Всё сразу
    python main.py all --input input.xlsx
"""
from __future__ import annotations

import argparse
from pathlib import Path

import config
from excel_io import read_cases
from scraper_runner import add_court_type, scrape_all, merge_with_source
from analyze import write_report


def cmd_classify(args):
    df = read_cases(args.input, sheets=args.sheets)
    df = add_court_type(df)
    print(f"Всего строк: {len(df)}\n")
    print(df["court_type"].value_counts().to_string())
    print()
    if args.show_unknown:
        unknown = df[df["court_type"].isin(["unknown", "empty"])]
        if not unknown.empty:
            print("Строки без распознанного типа суда (проверьте вручную):")
            print(unknown[["row_id", "sheet_name", "case_number", "link"]].to_string())


def cmd_scrape(args):
    df = read_cases(args.input, sheets=args.sheets)
    df = add_court_type(df)
    court_types = args.court_types.split(",") if args.court_types else None
    scraped = scrape_all(df, court_types=court_types, use_cache=not args.no_cache, limit=args.limit)
    if scraped.empty:
        print("Нет данных для скрапинга (проверьте --court-types и наличие ссылок)")
        return
    merged = merge_with_source(df, scraped)
    out_path = args.output or (config.OUTPUT_DIR / "scraped_merged.xlsx")
    merged.to_excel(out_path, index=False)
    n_ok = int(merged.get("scraped_fetched_ok", 0).fillna(False).sum()) if "scraped_fetched_ok" in merged else 0
    print(f"Готово: {len(scraped)} дел спарсено, {n_ok} успешно.")
    print(f"Результат сохранён: {out_path}")


def cmd_analyze(args):
    df = read_cases(args.input, sheets=args.sheets)
    df = add_court_type(df)
    # Если раньше уже скрапили — можно передать смёрженный файл через --merged
    if args.merged:
        import pandas as pd
        df = pd.read_excel(args.merged)
    out_path = args.output or (config.OUTPUT_DIR / "report.xlsx")
    write_report(df, out_path)
    print(f"Отчёт сохранён: {out_path}")


def cmd_all(args):
    cmd_scrape(args)
    args.merged = config.OUTPUT_DIR / "scraped_merged.xlsx"
    cmd_analyze(args)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path, default=config.DEFAULT_INPUT_XLSX)
    p.add_argument("--sheets", nargs="*", default=None, help="Ограничить конкретными листами (по умолчанию — все)")

    sub = p.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="Разбить ссылки по типам судов, без скрапинга")
    p_classify.add_argument("--show-unknown", action="store_true")
    p_classify.set_defaults(func=cmd_classify)

    p_scrape = sub.add_parser("scrape", help="Спарсить данные с сайтов судов")
    p_scrape.add_argument("--court-types", help="напр. general_jurisdiction_sudrf,arbitrazh_kad")
    p_scrape.add_argument("--limit", type=int, help="ограничить число дел на тип суда (для теста)")
    p_scrape.add_argument("--no-cache", action="store_true", help="игнорировать кэш, скачать заново")
    p_scrape.add_argument("--output", type=Path)
    p_scrape.set_defaults(func=cmd_scrape)

    p_analyze = sub.add_parser("analyze", help="Собрать xlsx-отчёт")
    p_analyze.add_argument("--merged", type=Path, help="файл из шага scrape (scraped_merged.xlsx)")
    p_analyze.add_argument("--output", type=Path)
    p_analyze.set_defaults(func=cmd_analyze)

    p_all = sub.add_parser("all", help="scrape + analyze за один прогон")
    p_all.add_argument("--court-types")
    p_all.add_argument("--limit", type=int)
    p_all.add_argument("--no-cache", action="store_true")
    p_all.add_argument("--output", type=Path)
    p_all.set_defaults(func=cmd_all)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
