"""
Общая конфигурация парсера судебных дел.
"""
from pathlib import Path

# --- Пути ---
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"           # сырые HTML/JSON по каждому делу (кэш)
OUTPUT_DIR = DATA_DIR / "output"     # итоговые xlsx/отчёты
DB_PATH = DATA_DIR / "cases.sqlite3" # структурированные данные по делам

for d in (RAW_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- Входная таблица ---
# Можно переопределить флагом --input у main.py
DEFAULT_INPUT_XLSX = BASE_DIR / "input.xlsx"
DEFAULT_SHEETS = None  # None = все листы; либо список имён листов

# Ожидаемые (или похожие) заголовки колонок во входной таблице.
# Сопоставление делается по нормализованному (strip + lower) названию,
# так что мелкие расхождения в пробелах не страшны.
COLUMN_ALIASES = {
    "num": ["номер", "№"],
    "company": ["наименование"],
    "party_role": ["сторона дела"],
    "instance": ["судебная инстанция"],
    "case_number": ["номер дела"],
    "court_name": ["наименование суда"],
    "opponent": ["наименование сторон"],
    "link": ["ссылка"],
    "summary": ["краткая информация"],
    "hearing_date": ["дата судебного заседания"],
    "next_action": ["процессуальное действие/срок", "процессуальное действие"],
    "result": ["итог"],
    "claim_amount": ["сумма заявленных требований"],
    "amount": ["сумма"],
    "financial_result": ["финасовый результат", "финансовый результат"],
    "year_closed": ["год завершения спора"],
}

# --- Классификация ссылок ---
# Порядок важен: первое совпадение побеждает.
COURT_TYPE_PATTERNS = [
    ("arbitrazh_kad", [r"kad\.arbitr\.ru"]),
    ("moscow_city", [r"mos-gorsud\.ru"]),
    # мировые судьи — тоже ГАС "Правосудие", но домен msudrf.ru, а не sudrf.ru
    ("magistrate_msudrf", [r"\.msudrf\.ru"]),
    ("general_jurisdiction_sudrf", [r"\.sudrf\.ru"]),
]

# --- Сетевые настройки / вежливый скрапинг ---
REQUEST_TIMEOUT = 30          # сек
MIN_DELAY_SEC = 1.5           # минимальная пауза между запросами к одному домену
MAX_DELAY_SEC = 3.5
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Playwright: показывать браузер (True полезно при первой отладке
# и если сайт требует пройти проверку "я не робот" руками)
PLAYWRIGHT_HEADLESS = True
PLAYWRIGHT_NAV_TIMEOUT_MS = 45_000
