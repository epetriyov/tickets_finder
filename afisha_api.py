"""Работа с API виджета Яндекс Билетов (widget.afisha.yandex.ru).

Структура API выяснена 15.07.2026 перехватом запросов реального виджета
(iframe на странице https://afisha.yandex.ru/moscow/concert/basta-2026-08-29):

- GET /api/tickets/v1/sessions/{SESSION_KEY}/hallplan/async?clientKey={CLIENT_KEY}
  -> JSON со ВСЕМИ свободными местами:
     result.hallplan.levels[]           — сектора зала
       .name                            — например "Сектор A206 (ограниченная видимость)"
       .admission                       — true для танцпола/фан-зоны (без нумерованных мест)
       .seats[]                         — только СВОБОДНЫЕ места
         .seat.row / .seat.place        — ряд и место (строки!)
         .sourceSeatId                  — уникальный id места, напр. "1351990-2-7"
         .priceInfo.price.value         — цена билета в КОПЕЙКАХ
         .priceInfo.total.value         — цена с сервисным сбором в КОПЕЙКАХ
  Занятых мест в ответе нет: availableSeatCount ~= сумма len(seats) по секторам.

- GET /api/tickets/v3/sessions/{SESSION_KEY}/seat-locks?clientKey={CLIENT_KEY}
  -> result.lockedPlaces — места, временно удерживаемые в чужих корзинах.

SESSION_KEY — base64 от "2966|732357|3292147|1788019200000"
(последнее число — timestamp сеанса в мс; 1788019200000 = 29.08.2026 19:00 МСК).

ЕСЛИ ФОРМАТ ОТВЕТА ИЗМЕНИТСЯ (Яндекс обновит виджет) — открой страницу концерта
в браузере, DevTools -> Network -> фильтр "hallplan", и сверь структуру ответа
с описанной выше. Править нужно в первую очередь extract_seats().
"""

import logging
import re

import requests

log = logging.getLogger("watcher.api")

WIDGET_BASE = "https://widget.afisha.yandex.ru"

# Обычные браузерные заголовки, чтобы не выделяться на фоне реального виджета.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class HallplanError(Exception):
    """Не удалось получить/разобрать схему зала."""


# Кириллические двойники латинских букв: пользователь может написать "С134"
# русской С — нормализуем, чтобы фильтр не промахнулся.
_CYRILLIC_LOOKALIKES = str.maketrans("АВСЕНКМОРТУХ", "ABCEHKMOPTYX")

_SECTOR_CODE_RE = re.compile(r"([A-Za-zА-Яа-я])\s?(\d+)")


def _norm_letter(ch):
    return ch.upper().translate(_CYRILLIC_LOOKALIKES)


def parse_sectors(spec):
    """Разбирает SECTORS из .env: "C134-C139,A109-A112" или "C134,A110".

    Возвращает список (буква, от, до) или None, если фильтр не задан.
    Непонятный формат — ValueError (лучше упасть на старте, чем молча
    мониторить не те сектора).
    """
    spec = (spec or "").strip()
    if not spec:
        return None
    out = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(
            r"([A-Za-zА-Яа-я])\s?(\d+)(?:\s?-\s?([A-Za-zА-Яа-я])?\s?(\d+))?", token
        )
        if not m:
            raise ValueError("непонятный сектор в SECTORS: {!r}".format(token))
        letter = _norm_letter(m.group(1))
        if m.group(3) and _norm_letter(m.group(3)) != letter:
            raise ValueError("в диапазоне разные буквы секторов: {!r}".format(token))
        lo = int(m.group(2))
        hi = int(m.group(4)) if m.group(4) else lo
        out.append((letter, min(lo, hi), max(lo, hi)))
    return out or None


def sector_matches(level_name, allowed):
    """True, если сектор из названия уровня попадает в фильтр allowed.

    allowed=None — фильтра нет, подходит всё. Уровень без кода сектора
    в названии ("Танцпол") при включённом фильтре не подходит.
    """
    if not allowed:
        return True
    m = _SECTOR_CODE_RE.search(level_name)
    if not m:
        return False
    letter, num = _norm_letter(m.group(1)), int(m.group(2))
    return any(letter == lt and lo <= num <= hi for lt, lo, hi in allowed)


def fetch_hallplan(session_key, client_key, timeout=30):
    """Возвращает объект result.hallplan из ответа API (dict).

    Бросает HallplanError при любой проблеме — вызывающий код логирует
    и ждёт следующую итерацию.
    """
    url = "{}/api/tickets/v1/sessions/{}/hallplan/async".format(WIDGET_BASE, session_key)
    try:
        resp = requests.get(
            url,
            params={"clientKey": client_key},
            headers=HEADERS,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise HallplanError("сетевая ошибка: {}".format(exc)) from exc

    if resp.status_code != 200:
        raise HallplanError("HTTP {}".format(resp.status_code))

    try:
        data = resp.json()
    except ValueError as exc:
        # Скорее всего отдали HTML (капча/заглушка) вместо JSON
        raise HallplanError("ответ не JSON (капча/антибот?)") from exc

    if data.get("status") != "success":
        raise HallplanError("status={!r}".format(data.get("status")))

    try:
        return data["result"]["hallplan"]
    except (KeyError, TypeError) as exc:
        raise HallplanError("нет result.hallplan в ответе") from exc


def fetch_locked_seat_ids(session_key, client_key, timeout=30):
    """Множество sourceSeatId мест, временно заблокированных в чужих корзинах.

    Ошибки здесь не критичны (вернём пустое множество): место из чужой корзины
    и так не купить, а через минуту-другую блокировка либо снимется, либо
    место пропадёт из hallplan.
    """
    url = "{}/api/tickets/v3/sessions/{}/seat-locks".format(WIDGET_BASE, session_key)
    try:
        resp = requests.get(
            url,
            params={"clientKey": client_key},
            headers=HEADERS,
            timeout=timeout,
        )
        data = resp.json()
        locked = data["result"]["lockedPlaces"]
    except Exception as exc:  # noqa: BLE001 — любой сбой не критичен
        log.warning("seat-locks: не удалось получить (%s), считаем что блокировок нет", exc)
        return set()

    ids = set()
    for item in locked:
        # На момент разведки список был пуст, поэтому формат элементов не
        # подтверждён. Обрабатываем оба вероятных варианта: строка-id или dict.
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            for key in ("sourceSeatId", "seatId", "id"):
                if key in item:
                    ids.add(str(item[key]))
                    break
    return ids


def extract_seats(hallplan, locked_ids=frozenset(), ignore_limited_view=False,
                  allowed_sectors=None):
    """Разворачивает hallplan в плоский список свободных мест.

    allowed_sectors — результат parse_sectors(); None = без фильтра по секторам.
    Возвращает список dict:
      {"level": str, "row": str, "place": int, "price": int, "total": int, "seat_id": str}
    Цены — в РУБЛЯХ. Места с ненумеруемым place (например "7А") пропускаются
    с warning-ом: для поиска соседних мест нужна числовая нумерация.
    """
    seats = []
    for level in hallplan.get("levels", []):
        if level.get("admission"):
            continue  # танцпол/фан-зона — нет нумерованных мест
        name = level.get("name", "?")
        if not sector_matches(name, allowed_sectors):
            continue
        if ignore_limited_view and "ограниченная видимость" in name.lower():
            continue
        for s in level.get("seats") or []:
            try:
                info = s["seat"]
                price_info = s["priceInfo"]
                seat = {
                    "level": name,
                    "level_id": level.get("id"),
                    "row": str(info["row"]),
                    "place": int(str(info["place"])),
                    "price": price_info["price"]["value"] // 100,
                    "total": price_info["total"]["value"] // 100,
                    "seat_id": str(s.get("sourceSeatId", "")),
                }
            except (KeyError, TypeError):
                log.warning("место с неожиданной структурой, пропускаю: %.200s", s)
                continue
            except ValueError:
                # place не число ("7А" и т.п.) — соседство не определить
                log.debug("нечисловой номер места %r в %s, пропускаю", info.get("place"), name)
                continue
            if seat["seat_id"] and seat["seat_id"] in locked_ids:
                continue
            seats.append(seat)
    return seats


def find_runs(seats, max_price, seats_needed=2):
    """Ищет цепочки из >= seats_needed СОСЕДНИХ свободных мест в одном ряду,
    где каждое место не дороже max_price рублей.

    Возвращает список dict, отсортированный по УБЫВАНИЮ цены (пользователь
    предпочитает более дорогие варианты):
      {"level", "level_id", "row", "places": [int, ...], "seats": [seat, ...],
       "price_min", "price_max", "total_min", "total_max",
       "seat_keys": ["level|row|place", ...]}
    Пересекающиеся пары схлопнуты в одну цепочку: места 8,9,10 подряд — это
    одна запись places=[8,9,10], а не пары (8,9) и (9,10).
    """
    by_row = {}
    for s in seats:
        if s["price"] > max_price:
            continue
        by_row.setdefault((s["level"], s["row"]), {})[s["place"]] = s

    runs = []
    for (level, row), row_seats in by_row.items():
        places = sorted(row_seats)
        chain = []
        for p in places + [None]:  # None — терминатор, чтобы закрыть последнюю цепочку
            if chain and (p is None or p != chain[-1] + 1):
                if len(chain) >= seats_needed:
                    group = [row_seats[x] for x in chain]
                    runs.append({
                        "level": level,
                        "level_id": group[0].get("level_id"),
                        "row": row,
                        "places": list(chain),
                        "seats": group,
                        "price_min": min(g["price"] for g in group),
                        "price_max": max(g["price"] for g in group),
                        "total_min": min(g["total"] for g in group),
                        "total_max": max(g["total"] for g in group),
                        "seat_keys": ["{}|{}|{}".format(level, row, x) for x in chain],
                    })
                chain = []
            if p is not None:
                chain.append(p)

    def row_sort_key(row):
        try:
            return (0, int(row))
        except ValueError:
            return (1, row)

    # Дорогие варианты — первыми (пожелание пользователя)
    runs.sort(key=lambda r: (-r["price_min"], r["level"], row_sort_key(r["row"])))
    return runs


def buy_link(session_key, run, seats_needed=2):
    """Прямая ссылка на виджет с уже добавленными в корзину местами.

    Формат подтверждён экспериментально 16.07.2026: параметр selectedSeats —
    URL-encoded JSON [{"level": <id уровня>, "row": "...", "place": "..."}].
    Виджет добавляет места в корзину, только если ВСЕ они ещё свободны,
    иначе просто откроется схема — это ок.

    Из цепочки берём seats_needed ПОДРЯД идущих мест с максимальной суммарной
    ценой (пользователь предпочитает более дорогие).
    """
    import json as _json
    from urllib.parse import quote

    seats = run["seats"]
    n = min(seats_needed, len(seats))
    best = max(
        (seats[i:i + n] for i in range(len(seats) - n + 1)),
        key=lambda w: sum(s["price"] for s in w),
    )
    payload = _json.dumps(
        [{"level": s["level_id"], "row": s["row"], "place": str(s["place"])} for s in best],
        separators=(",", ":"), ensure_ascii=False,
    )
    return "{}/w/sessions/{}?selectedSeats={}".format(
        WIDGET_BASE, session_key, quote(payload, safe="")
    )
