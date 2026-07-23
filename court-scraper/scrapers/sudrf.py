"""
Скрапер для *.sudrf.ru — сотен региональных судов общей юрисдикции на типовом
движке ГАС "Правосудие" (URL вида .../modules.php?name=sud_delo&...).

Эти страницы отдаются сервером как обычный HTML (без JS-рендеринга), поэтому
здесь используется requests + BeautifulSoup — быстро и без браузера.

ВАЖНО (сделать при первом запуске в Claude Code, у которого есть интернет):
  1. Открыть 2-3 реальные ссылки из вашей таблицы руками в браузере и посмотреть
     верстку (Ctrl+U / F12), т.к. верстка чуть отличается между версиями движка
     на разных сайтах судов.
  2. Прогнать `python main.py smoke-test --court-type general_jurisdiction_sudrf`
     (см. README) на 2-3 строках и свериться с оригиналом.
  3. При необходимости поправить LABELS ниже — экстрактор ищет пары
     "подпись: значение" по тексту, а не по CSS-классам, поэтому обычно
     достаточно донастроить сами подписи (они бывают "Судья:" / "Судья" /
     "Судья по делу:" и т.п. в разных инсталляциях).

Страница обычно содержит блоки:
  - "Информация по делу" / "Основная информация" — номер дела, судья, дата
    поступления, результат рассмотрения;
  - "Стороны по делу" — таблица с ролями участников (Истец/Ответчик/...);
  - "Движение дела" — таблица событий по датам (назначено, состоялось,
    результат, следующее заседание).
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import config
from scrapers.base import CaseData, CaseEvent, RawCache, Throttler, retry

LABELS = {
    "judge": ["судья"],
    "filed_date": ["дата поступления", "дата регистрации"],
    "status": ["результат рассмотрения", "стадия", "статус"],
    "case_number": ["номер дела", "дело №", "№ дела"],
    "claim_subject": ["категория дела", "предмет", "суть иска"],
}

PARTY_ROLE_KEYWORDS = ["истец", "ответчик", "заявитель", "заинтересованное лицо", "третье лицо"]

session = requests.Session()
session.headers.update({"User-Agent": config.USER_AGENT})
_throttler = Throttler()
_cache = RawCache()


def _normalize_label(s: str) -> str:
    return re.sub(r"[:\s]+$", "", s.strip().lower())


def _extract_label_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """
    Проходит по всем таблицам страницы и собирает пары label -> value из строк
    вида <tr><td>Подпись:</td><td>Значение</td></tr>, а также из <dt>/<dd>.
    Работает независимо от конкретных CSS-классов движка.
    """
    pairs: dict[str, str] = {}

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 2:
            label = cells[0].get_text(" ", strip=True)
            value = cells[1].get_text(" ", strip=True)
            if label and value and len(label) < 60:
                pairs[_normalize_label(label)] = value

    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs[_normalize_label(dt.get_text(" ", strip=True))] = dd.get_text(" ", strip=True)

    return pairs


def _find_by_labels(pairs: dict[str, str], candidates: list[str]) -> str | None:
    for cand in candidates:
        cand_n = _normalize_label(cand)
        for label, value in pairs.items():
            if cand_n in label:
                return value
    return None


def _extract_parties(soup: BeautifulSoup) -> tuple[str | None, str | None, list[str]]:
    plaintiff = defendant = None
    third_parties: list[str] = []

    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True).lower()
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        role_cell = cells[0].get_text(" ", strip=True).lower()
        name_cell = cells[1].get_text(" ", strip=True)
        if not name_cell:
            continue
        if "истец" in role_cell or "заявител" in role_cell:
            plaintiff = plaintiff or name_cell
        elif "ответчик" in role_cell:
            defendant = defendant or name_cell
        elif "треть" in role_cell or "заинтересован" in role_cell:
            third_parties.append(name_cell)

    return plaintiff, defendant, third_parties


_DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_EVENT_HEADER_LABELS = {"дата", "наименование события", "событие"}


def _extract_events(soup: BeautifulSoup) -> list[CaseEvent]:
    """
    Ищет таблицу "Движение дела". На реальных страницах ГАС "Правосудие"
    (проверено на живых делах) это пары <tr><td>Название события</td>
    <td>дд.мм.гггг</td></tr> — т.е. дата во ВТОРОЙ ячейке, а не в первой,
    как можно было бы предположить (первая ячейка — название события вроде
    "Регистрация иска в суде", "Судебное заседание", "Изготовлено
    мотивированное решение").
    """
    events = []
    seen = set()
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True)
        value = cells[1].get_text(" ", strip=True)
        if not label or _normalize_label(label) in _EVENT_HEADER_LABELS:
            continue
        m = _DATE_RE.search(value)
        if not m:
            continue
        key = (label, m.group())
        if key in seen:
            continue
        seen.add(key)
        events.append(CaseEvent(date=m.group(), title=label))
    return events


def _extract_case_number(soup: BeautifulSoup) -> str | None:
    """
    Номер дела в привычном формате (напр. "2-3772/2024") обычно выводится
    отдельным заголовком <div class="casenumber">ДЕЛО № 2-3772/2024 ~
    М-1247/2024</div>, а не таблицей "подпись: значение" — таблица чаще
    содержит только служебный УИД дела (напр. "35RS0010-01-2024-002061-11").
    """
    div = soup.find(class_="casenumber")
    if div:
        text = div.get_text(" ", strip=True)
        m = re.search(r"№\s*([^\s~]+(?:\s*~\s*[^\s]+)?)", text)
        if m:
            return m.group(1).strip()
        return text or None
    return None


def _extract_instance(soup: BeautifulSoup) -> str | None:
    """Заголовок раздела (напр. "Гражданские дела - первая инстанция")."""
    div = soup.find(class_="title")
    if div:
        text = div.get_text(" ", strip=True)
        return text or None
    return None


def _next_hearing_date(events: list[CaseEvent]) -> str | None:
    """Ближайшая дата из "Движения дела", которая ещё не наступила."""
    today = datetime.now().date()
    upcoming = []
    for e in events:
        if not e.date:
            continue
        try:
            d = datetime.strptime(e.date, "%d.%m.%Y").date()
        except ValueError:
            continue
        if d >= today:
            upcoming.append(d)
    if not upcoming:
        return None
    return min(upcoming).strftime("%d.%m.%Y")


def fetch(url: str, use_cache: bool = True, court_type: str = "general_jurisdiction_sudrf") -> CaseData:
    """
    court_type: "general_jurisdiction_sudrf" (районные/областные суды, *.sudrf.ru)
                или "magistrate_msudrf" (мировые судьи, *.msudrf.ru) — верстка та же,
                это тот же движок ГАС "Правосудие", отличается только домен.
    """
    domain = urlparse(url).netloc
    case = CaseData(source_url=url, court_type=court_type)

    if use_cache:
        cached = _cache.get(case.court_type, url)
        if cached:
            return _case_from_cached_html(url, cached["html"], court_type=court_type)

    def _do_request():
        _throttler.wait(domain)
        resp = session.get(url, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        return resp.text

    try:
        html = retry(_do_request, on_error=lambda a, e: print(f"[sudrf] попытка {a} для {url} не удалась: {e}"))
    except Exception as e:
        case.error = str(e)
        return case

    _cache.set(case.court_type, url, {"html": html})
    return _case_from_cached_html(url, html, court_type=court_type)


def _case_from_cached_html(url: str, html: str, court_type: str = "general_jurisdiction_sudrf") -> CaseData:
    case = CaseData(source_url=url, court_type=court_type)
    soup = BeautifulSoup(html, "html.parser")
    body_text_lower = soup.get_text(" ", strip=True).lower()

    # Два реальных ответа сайтов ГАС "Правосудие", которые выглядят как
    # валидный HTML (200 OK), но не содержат данных по делу — их надо
    # отличать от "верстка поменялась", иначе непонятно, что чинить:
    if "captcha" in html.lower() or "дополнительную проверку" in body_text_lower:
        case.error = ("Сайт суда показал капчу (проверка \"я не робот\") вместо данных дела — "
                      "снизьте частоту запросов и повторите позже; полностью автоматически "
                      "капчу не обходим")
        return case
    if "информация временно недоступна" in body_text_lower:
        # Проверено на практике: это временный сбой конкретно этого сайта
        # (буквально то же дело через некоторое время отдавалось нормально),
        # а не признак того, что модуль отключён навсегда — не проблема
        # наших селекторов, повторите позже.
        case.error = ("Сайт суда временно отдаёт \"Информация временно недоступна\" вместо "
                      "данных дела (не проблема наших селекторов) — по наблюдению, это "
                      "проходит само через какое-то время, повторите позже")
        return case

    pairs = _extract_label_value_pairs(soup)
    case.judge = _find_by_labels(pairs, LABELS["judge"])
    case.filed_date = _find_by_labels(pairs, LABELS["filed_date"])
    case.status = _find_by_labels(pairs, LABELS["status"])
    case.claim_subject = _find_by_labels(pairs, LABELS["claim_subject"])

    case.case_number = _extract_case_number(soup) or _find_by_labels(pairs, LABELS["case_number"])
    case.instance = _extract_instance(soup)

    case.plaintiff, case.defendant, case.third_parties = _extract_parties(soup)
    case.events = _extract_events(soup)
    case.next_hearing_date = _next_hearing_date(case.events)
    if case.events:
        # События идут в порядке чтения страницы (сначала первая инстанция,
        # потом апелляция), а не строго по дате — поэтому для "последнего"
        # события берём максимум по дате, а не последний элемент списка.
        def _event_date(e: CaseEvent):
            try:
                return datetime.strptime(e.date, "%d.%m.%Y")
            except (ValueError, TypeError):
                return datetime.min

        last = max(case.events, key=_event_date)
        case.last_hearing_result = last.title

    # Если вообще ничего не извлеклось — вероятно, изменилась верстка
    # или страница вернула "дело не найдено" / капчу.
    if not any([case.judge, case.status, case.plaintiff, case.defendant, case.events]):
        case.error = "Не удалось извлечь данные — проверьте верстку страницы вручную"
    else:
        case.fetched_ok = True

    return case
