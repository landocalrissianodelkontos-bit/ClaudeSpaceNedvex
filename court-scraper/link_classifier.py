"""
Определяет тип сайта суда по ссылке.

Известные семейства на сегодня (проверено на реальной выгрузке):
  - arbitrazh_kad            -> kad.arbitr.ru (картотека арбитражных дел)
  - moscow_city              -> mos-gorsud.ru (Мосгорсуд, отдельная СПА-система)
  - general_jurisdiction_sudrf -> *.sudrf.ru (сотни поддоменов райсудов/облсудов
                                  на типовом движке ГАС "Правосудие")
  - unknown                  -> всё остальное / пусто

Если во входных данных попадутся новые домены судов, которых здесь нет —
добавьте паттерн в config.COURT_TYPE_PATTERNS, отдельный скрапер писать
не обязательно, если структура страницы похожа на одну из трёх известных.
"""
import re
from urllib.parse import urlparse

from config import COURT_TYPE_PATTERNS


def classify_link(url) -> str:
    try:
        import pandas as pd
        if url is None or (isinstance(url, float) and pd.isna(url)):
            return "empty"
    except ImportError:
        if url is None:
            return "empty"
    if not str(url).strip():
        return "empty"
    url = str(url).strip()
    for court_type, patterns in COURT_TYPE_PATTERNS:
        for pat in patterns:
            if re.search(pat, url, re.IGNORECASE):
                return court_type
    return "unknown"


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


if __name__ == "__main__":
    tests = [
        "https://kad.arbitr.ru/Card/7175d728-0408-4e26-a15f-9bf94f1a895a",
        "https://sochi-xostinsky--krd.sudrf.ru/modules.php?name=sud_delo",
        "https://mos-gorsud.ru/rs/gagarinskij/services/cases/claim-civil/details/xxx",
        "",
        None,
        "https://example.com",
    ]
    for t in tests:
        print(t, "->", classify_link(t))
