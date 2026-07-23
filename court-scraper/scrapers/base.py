"""
Общая инфраструктура для всех скраперов:
  - CaseData: унифицированная структура результата (что бы мы ни спарсили —
    из sudrf.ru, kad.arbitr.ru или mos-gorsud.ru — на выходе одна и та же форма);
  - Throttler: вежливые паузы между запросами к одному домену;
  - RawCache: кэш сырых ответов на диске, чтобы не дёргать сайт повторно
    при перезапуске (и чтобы всегда можно было посмотреть, что реально пришло).
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import config


@dataclass
class CaseEvent:
    date: Optional[str] = None
    title: Optional[str] = None          # напр. "Судебное заседание", "Решение"
    result: Optional[str] = None         # что произошло / резолюция
    document_url: Optional[str] = None   # ссылка на текст акта/протокола, если есть


@dataclass
class CaseData:
    source_url: str
    court_type: str                       # arbitrazh_kad / general_jurisdiction_sudrf / moscow_city
    fetched_ok: bool = False
    error: Optional[str] = None

    case_number: Optional[str] = None
    court_name: Optional[str] = None
    judge: Optional[str] = None
    instance: Optional[str] = None
    status: Optional[str] = None          # текущий статус дела на сайте суда
    plaintiff: Optional[str] = None
    defendant: Optional[str] = None
    third_parties: list[str] = field(default_factory=list)
    claim_subject: Optional[str] = None   # предмет иска
    filed_date: Optional[str] = None      # дата поступления/регистрации
    next_hearing_date: Optional[str] = None
    last_hearing_result: Optional[str] = None
    events: list[CaseEvent] = field(default_factory=list)   # движение по делу
    documents: list[dict] = field(default_factory=list)      # [{title, url, date}]

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class Throttler:
    """Хранит время последнего запроса по домену и ждёт нужную паузу перед следующим."""

    def __init__(self):
        self._last_request: dict[str, float] = {}

    def wait(self, domain: str):
        now = time.monotonic()
        last = self._last_request.get(domain)
        delay = random.uniform(config.MIN_DELAY_SEC, config.MAX_DELAY_SEC)
        if last is not None:
            elapsed = now - last
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_request[domain] = time.monotonic()


class RawCache:
    """Кэширует сырой ответ по URL на диске в data/raw/<court_type>/<hash>.json"""

    def __init__(self, root: Path = config.RAW_DIR):
        self.root = root

    def _path(self, court_type: str, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        d = self.root / court_type
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{h}.json"

    def get(self, court_type: str, url: str) -> Optional[dict]:
        p = self._path(court_type, url)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def set(self, court_type: str, url: str, payload: dict):
        p = self._path(court_type, url)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def retry(fn, *, max_retries=None, backoff=None, on_error=None):
    """Простой ретрай с линейным бэкоффом. Используется скраперами вокруг сетевых вызовов."""
    max_retries = max_retries or config.MAX_RETRIES
    backoff = backoff or config.RETRY_BACKOFF_SEC
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - здесь осознанно ловим всё сетевое
            last_exc = e
            if on_error:
                on_error(attempt, e)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
    raise last_exc
