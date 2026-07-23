"""
Прогоняет DataFrame со строками таблицы через нужный скрапер по типу суда
(колонка court_type, см. link_classifier) и возвращает DataFrame с
результатами парсинга, готовый к склейке с исходной таблицей и к анализу.
"""
from __future__ import annotations

import pandas as pd

try:
    from tqdm import tqdm
except ImportError:  # tqdm необязателен — просто нет прогресс-бара
    def tqdm(iterable, **kwargs):
        return iterable

from link_classifier import classify_link
from scrapers import sudrf, kad_arbitr, mos_gorsud
from scrapers.base import CaseData


def add_court_type(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["court_type"] = df["link"].apply(classify_link)
    return df


def scrape_all(df: pd.DataFrame, court_types: list[str] | None = None,
                use_cache: bool = True, limit: int | None = None) -> pd.DataFrame:
    """
    df должен содержать колонки: link, court_type (см. add_court_type).
    court_types — если задано, скрапим только эти типы (напр. только
    "general_jurisdiction_sudrf" для быстрого теста).
    limit — ограничить число строк на тип (для смок-теста).
    """
    if "court_type" not in df.columns:
        df = add_court_type(df)

    results: list[CaseData] = []

    # --- sudrf.ru и msudrf.ru (мировые судьи): строка за строкой, requests-based ---
    for ct, label in [("general_jurisdiction_sudrf", "sudrf.ru"),
                       ("magistrate_msudrf", "msudrf.ru (мировые судьи)")]:
        rows = df[df["court_type"] == ct]
        if court_types is None or ct in court_types:
            urls = rows["link"].dropna().unique().tolist()
            if limit:
                urls = urls[:limit]
            for url in tqdm(urls, desc=label):
                results.append(sudrf.fetch(url, use_cache=use_cache, court_type=ct))

    # --- kad.arbitr.ru: пачкой через один браузер (Playwright) ---
    kad_rows = df[df["court_type"] == "arbitrazh_kad"]
    if court_types is None or "arbitrazh_kad" in court_types:
        urls = kad_rows["link"].dropna().unique().tolist()
        if limit:
            urls = urls[:limit]
        if urls:
            results.extend(kad_arbitr.fetch_many(urls, use_cache=use_cache))

    # --- mos-gorsud.ru: пачкой через один браузер (Playwright) ---
    mgs_rows = df[df["court_type"] == "moscow_city"]
    if court_types is None or "moscow_city" in court_types:
        urls = mgs_rows["link"].dropna().unique().tolist()
        if limit:
            urls = urls[:limit]
        if urls:
            results.extend(mos_gorsud.fetch_many(urls, use_cache=use_cache))

    if not results:
        return pd.DataFrame()

    records = [r.to_dict() for r in results]
    result_df = pd.DataFrame(records)
    result_df = result_df.add_prefix("scraped_")
    result_df = result_df.rename(columns={"scraped_source_url": "link"})
    return result_df


def merge_with_source(source_df: pd.DataFrame, scraped_df: pd.DataFrame) -> pd.DataFrame:
    """Джойнит исходную таблицу со спарсенными данными по ссылке (link)."""
    if scraped_df.empty:
        return source_df
    return source_df.merge(scraped_df, on="link", how="left")
