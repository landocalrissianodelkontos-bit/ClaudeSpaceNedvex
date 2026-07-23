"""
Скрапер для mos-gorsud.ru — у Мосгорсуда своя система, отдельная от типового
движка ГАС "Правосудие" (*.sudrf.ru) и от kad.arbitr.ru. Тоже SPA (данные
подгружаются JS-ом), поэтому используется Playwright.

В вашей таблице таких ссылок немного (5 шт.), но игнорировать их нельзя —
это реальные дела. Сделать перед боевым запуском (см. также докстринг в
kad_arbitr.py — тут те же соображения):

  1. `playwright install chromium` (если ещё не сделано)
  2. Открыть 2-3 реальные ссылки mos-gorsud.ru из таблицы с
     PLAYWRIGHT_HEADLESS=False, посмотреть DOM в DevTools.
  3. Поправить SELECTORS под реальную верстку.

URL-паттерны из вашей таблицы:
  https://mos-gorsud.ru/rs/<суд>/services/cases/civil/details/<uuid>
  https://mos-gorsud.ru/rs/<суд>/services/cases/claim-civil/details/<uuid>
  https://mos-gorsud.ru/mgs/services/cases/appeal-civil/details/<uuid>

т.е. в URL уже видно инстанцию (rs = районный суд, mgs = городской суд —
апелляция) и тип производства (civil / claim-civil / appeal-civil) — это
можно использовать для заполнения case.instance ещё до похода на сайт.
"""
from __future__ import annotations

import re

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import config
from scrapers.base import CaseData, CaseEvent, RawCache, Throttler, retry

_throttler = Throttler()
_cache = RawCache()

DOMAIN = "mos-gorsud.ru"

SELECTORS = {
    "case_number": "[class*=case-number], [class*=caseNumber], h1",
    "parties_block": "[class*=parties], [class*=participant]",
    "history_block": "[class*=history], [class*=movement], [class*=events]",
    "status": "[class*=status]",
}

INSTANCE_FROM_URL = [
    (re.compile(r"/mgs/"), "Апелляция (Мосгорсуд)"),
    (re.compile(r"/rs/.+/appeal"), "Апелляция"),
    (re.compile(r"/rs/"), "Первая инстанция (районный суд)"),
]


def _guess_instance(url: str) -> str | None:
    for pattern, label in INSTANCE_FROM_URL:
        if pattern.search(url):
            return label
    return None


def _extract_from_page(page, url: str) -> CaseData:
    case = CaseData(source_url=url, court_type="moscow_city")
    case.instance = _guess_instance(url)

    try:
        page.wait_for_selector("body", timeout=config.PLAYWRIGHT_NAV_TIMEOUT_MS)
        page.wait_for_timeout(1500)  # дать SPA время догрузить данные
    except PlaywrightTimeoutError:
        case.error = "Таймаут ожидания загрузки страницы"
        return case

    body_text = page.inner_text("body")
    if "не найден" in body_text.lower():
        case.error = "Дело не найдено по этой ссылке (проверьте вручную)"
        return case

    def text_or_none(selector):
        try:
            el = page.query_selector(selector)
            return el.inner_text().strip() if el else None
        except Exception:
            return None

    case.case_number = text_or_none(SELECTORS["case_number"])
    case.status = text_or_none(SELECTORS["status"])

    parties_text = text_or_none(SELECTORS["parties_block"])
    if parties_text:
        for line in parties_text.splitlines():
            low = line.lower()
            if "истец" in low or "заявитель" in low:
                case.plaintiff = line.split(":", 1)[-1].strip()
            elif "ответчик" in low:
                case.defendant = line.split(":", 1)[-1].strip()

    history_text = text_or_none(SELECTORS["history_block"])
    if history_text:
        for line in [l for l in history_text.splitlines() if l.strip()]:
            case.events.append(CaseEvent(title=line.strip()))
        if case.events:
            case.last_hearing_result = case.events[-1].title

    case.fetched_ok = any([case.case_number, case.status, case.plaintiff, case.defendant, case.events])
    if not case.fetched_ok:
        case.error = "Не удалось извлечь данные — проверьте SELECTORS в mos_gorsud.py"

    return case


def fetch_many(urls: list[str], use_cache: bool = True) -> list[CaseData]:
    results: list[CaseData] = []
    to_fetch = []

    for url in urls:
        if use_cache:
            cached = _cache.get("moscow_city", url)
            if cached:
                cd = CaseData(source_url=url, court_type="moscow_city")
                cd.__dict__.update(cached)
                results.append(cd)
                continue
        to_fetch.append(url)

    if not to_fetch:
        return results

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.PLAYWRIGHT_HEADLESS)
        page = browser.new_page(user_agent=config.USER_AGENT)
        page.set_default_navigation_timeout(config.PLAYWRIGHT_NAV_TIMEOUT_MS)

        for url in to_fetch:
            _throttler.wait(DOMAIN)

            def _do_nav():
                # "networkidle" ненадёжен на этом сайте (см. kad_arbitr.py) —
                # используем domcontentloaded + явную паузу/ожидание селектора.
                page.goto(url, wait_until="domcontentloaded")

            try:
                retry(_do_nav, on_error=lambda a, e: print(f"[mos_gorsud] попытка {a} для {url} не удалась: {e}"))
                case = _extract_from_page(page, url)
            except Exception as e:
                case = CaseData(source_url=url, court_type="moscow_city", error=str(e))

            _cache.set("moscow_city", url, case.to_dict())
            results.append(case)

        browser.close()

    return results
