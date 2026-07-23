"""
Скрапер для kad.arbitr.ru — картотека арбитражных дел.

Это Angular-приложение (данные подгружаются JS-ом после открытия страницы),
и у сайта есть защита от автоматических обращений (лимиты по частоте,
иногда — проверка "я не робот"). Поэтому здесь используется Playwright
(реальный headless-браузер), а не прямые HTTP-запросы к внутреннему API:
адреса внутреннего API у сайта время от времени меняются и не документированы
официально, а рендеринг через браузер устойчив к этим изменениям, т.к.
мы читаем то же DOM, что видит живой пользователь.

ВАЖНО — сделать это первым делом при запуске в Claude Code (там уже есть
интернет, здесь при разработке скрипта интернета не было, поэтому
CSS-селекторы ниже — это первое приближение, а не проверенный факт):

  1. `playwright install chromium`
  2. Открыть 2-3 реальные ссылки kad.arbitr.ru из вашей таблицы с
     PLAYWRIGHT_HEADLESS=False (см. config.py) и посмотреть, что реально
     на странице — через `page.content()` или руками в DevTools.
  3. Поправить селекторы в SELECTORS ниже под то, что увидите. Хорошие якоря —
     это обычно текстовые подписи ("Истец", "Ответчик", "Судья", "Дело №"),
     они меняются реже, чем CSS-классы.
  4. Если сайт показывает капчу/блокировку — увеличьте задержки в config.py
     (MIN_DELAY_SEC/MAX_DELAY_SEC), уменьшите частоту запросов, по возможности
     запускайте не более нескольких десятков дел за раз с паузами между
     запусками. Полностью автоматически капчу обходить не пытаемся — если
     она появляется стабильно, скрипт должен на это явно пожаловаться
     (см. CaseData.error), а не притворяться, что всё ок.

Ожидаемая структура карточки дела (типовая для Angular-версии kad.arbitr.ru):
  - Шапка: номер дела, суд, судья, дата поступления, статус движения дела.
  - Блок "Участники дела": роли и наименования сторон.
  - Блок "Инстанции" / "История рассмотрения": по каждой инстанции — суд,
    судья, результат.
  - Блок "Документы по делу": список актов/определений со ссылками на PDF.
"""
from __future__ import annotations

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import config
from scrapers.base import CaseData, CaseEvent, RawCache, Throttler, retry

_throttler = Throttler()
_cache = RawCache()

DOMAIN = "kad.arbitr.ru"

# Текстовые якоря для поиска блоков на странице. Подгоняются под реальную
# верстку на первом запуске (см. докстринг выше).
SELECTORS = {
    "case_number": "b.case-number, .case-header .num, h1",
    "parties_block": "div.b-case-parties, .parties, .b-parties",
    "instances_block": "div.b-case-instances, .instances, .case-instances",
    "documents_block": "div.b-case-documents, .case-documents, .b-documents",
    "status": ".case-status, .b-case-status",
}


def _extract_from_page(page, url: str) -> CaseData:
    case = CaseData(source_url=url, court_type="arbitrazh_kad")

    try:
        page.wait_for_selector("body", timeout=config.PLAYWRIGHT_NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        case.error = "Таймаут ожидания загрузки страницы"
        return case

    # Дело может быть недоступно / номер неверный
    body_text = page.inner_text("body")
    body_text_lower = body_text.lower()
    if "не найдено" in body_text_lower or "не существует" in body_text_lower:
        case.error = "Дело не найдено по этой ссылке (проверьте вручную)"
        return case
    # Реальный экран капчи kad.arbitr.ru ("pravocaptcha") НЕ содержит слова
    # "captcha" в видимом тексте (оно только в CSS-классах/JS) — по факту
    # там "Подтверждение действия" / "Введите код с картинки" / после
    # нескольких неверных попыток "Превышено количество попыток ввода кода".
    if any(marker in body_text_lower for marker in (
        "подтвердите, что вы не робот",
        "подтверждение действия",
        "код с картинки",
        "превышено количество попыток",
    )):
        case.error = "Сайт показал капчу/блокировку — снизьте частоту запросов и повторите позже"
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
        # Грубое деление по строкам "Истец: ..." / "Ответчик: ...".
        for line in parties_text.splitlines():
            low = line.lower()
            if "истец" in low or "заявитель" in low:
                case.plaintiff = line.split(":", 1)[-1].strip()
            elif "ответчик" in low:
                case.defendant = line.split(":", 1)[-1].strip()
            elif "трет" in low:
                case.third_parties.append(line.split(":", 1)[-1].strip())

    instances_text = text_or_none(SELECTORS["instances_block"])
    if instances_text:
        for line in [l for l in instances_text.splitlines() if l.strip()]:
            case.events.append(CaseEvent(title=line.strip()))
        if case.events:
            case.last_hearing_result = case.events[-1].title

    # Документы/акты по делу — ссылки на PDF
    try:
        doc_links = page.query_selector_all(f"{SELECTORS['documents_block']} a")
        for a in doc_links:
            href = a.get_attribute("href")
            title = a.inner_text().strip()
            if href:
                case.documents.append({"title": title, "url": href})
    except Exception:
        pass

    case.fetched_ok = any([case.case_number, case.status, case.plaintiff, case.defendant, case.events])
    if not case.fetched_ok:
        case.error = "Не удалось извлечь данные — вероятно, изменилась верстка, проверьте SELECTORS в kad_arbitr.py"

    return case


def fetch_many(urls: list[str], use_cache: bool = True) -> list[CaseData]:
    """
    Открывает один браузер на всю пачку ссылок (дешевле, чем на каждую ссылку
    поднимать новый процесс браузера).
    """
    results: list[CaseData] = []
    to_fetch = []

    for url in urls:
        if use_cache:
            cached = _cache.get("arbitrazh_kad", url)
            if cached:
                # Кэшируем сразу готовый CaseData как dict
                cd = CaseData(source_url=url, court_type="arbitrazh_kad")
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
                # "networkidle" ненадёжен на этом сайте: страница держит
                # фоновые long-polling запросы, поэтому сеть никогда не
                # "затихает" и goto виснет/рвётся по таймауту — используем
                # domcontentloaded + явные ожидания селекторов ниже.
                page.goto(url, wait_until="domcontentloaded")

            try:
                retry(_do_nav, on_error=lambda a, e: print(f"[kad_arbitr] попытка {a} для {url} не удалась: {e}"))
                case = _extract_from_page(page, url)
            except Exception as e:
                case = CaseData(source_url=url, court_type="arbitrazh_kad", error=str(e))

            _cache.set("arbitrazh_kad", url, case.to_dict())
            results.append(case)

        browser.close()

    return results
