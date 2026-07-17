"""Тесты чистой логики: разбор hallplan и поиск цепочек соседних мест.

Синтетический hallplan повторяет реальную структуру ответа
widget.afisha.yandex.ru (см. шапку afisha_api.py) — если API изменится
и extract_seats() придётся править, эти тесты зафиксируют контракт.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from urllib.parse import parse_qs, unquote, urlparse

import pytest

from afisha_api import (MSK, buy_link, extract_event_name, extract_seats,
                        extract_sessions, find_runs, parse_sectors,
                        pick_session, sector_matches)


def seat(row, place, price_rub, seat_id=None):
    return {
        "seat": {"place": str(place), "row": str(row)},
        "sourceSeatId": seat_id or "1-{}-{}".format(row, place),
        "priceInfo": {
            "price": {"currencyCode": "rub", "value": price_rub * 100},
            "total": {"currencyCode": "rub", "value": int(price_rub * 1.1) * 100},
        },
    }


def hallplan(*levels):
    return {"levels": list(levels), "availableSeatCount": 0}


def level(name, seats, admission=False, level_id=7):
    return {"name": name, "admission": admission, "seats": seats, "id": level_id}


def test_extract_seats_basic():
    hp = hallplan(level("Сектор B105", [seat(23, 5, 12000), seat(23, 6, 12000)]))
    seats = extract_seats(hp)
    assert len(seats) == 2
    assert seats[0] == {
        "level": "Сектор B105", "level_id": 7, "row": "23", "place": 5,
        "price": 12000, "total": 13200, "seat_id": "1-23-5",
    }


def test_extract_skips_admission_and_locked_and_nonnumeric():
    hp = hallplan(
        level("Танцпол", [seat(1, 1, 6600)], admission=True),
        level("Сектор A", [seat(2, "7А", 5000), seat(2, 8, 5000, seat_id="locked-1")]),
    )
    seats = extract_seats(hp, locked_ids={"locked-1"})
    assert seats == []


def test_extract_ignore_limited_view():
    hp = hallplan(
        level("Сектор A206 (ограниченная видимость)", [seat(31, 8, 3500), seat(31, 9, 3500)]),
        level("Сектор B105", [seat(23, 5, 12000), seat(23, 6, 12000)]),
    )
    assert len(extract_seats(hp)) == 4
    seats = extract_seats(hp, ignore_limited_view=True)
    assert {s["level"] for s in seats} == {"Сектор B105"}


def test_find_runs_collapses_adjacent_and_filters_price():
    hp = hallplan(level("Сектор A", [
        seat(31, 8, 3500), seat(31, 9, 3500), seat(31, 10, 3500),  # цепочка из 3
        seat(31, 12, 3500),                                        # одиночка — не пара
        seat(5, 1, 20000), seat(5, 2, 20000),                      # дороже лимита
    ]))
    runs = find_runs(extract_seats(hp), max_price=15000, seats_needed=2)
    assert len(runs) == 1
    run = runs[0]
    assert run["row"] == "31"
    assert run["places"] == [8, 9, 10]
    assert run["price_min"] == run["price_max"] == 3500
    assert run["seat_keys"] == ["Сектор A|31|8", "Сектор A|31|9", "Сектор A|31|10"]


def test_max_price_is_inclusive():
    hp = hallplan(level("Сектор A", [
        seat(1, 1, 15000), seat(1, 2, 15000),   # ровно на границе — подходят
        seat(2, 1, 15001), seat(2, 2, 15001),   # на рубль дороже — нет
    ]))
    runs = find_runs(extract_seats(hp), max_price=15000, seats_needed=2)
    assert [(r["row"], r["price_min"]) for r in runs] == [("1", 15000)]


def test_find_runs_respects_seats_needed():
    hp = hallplan(level("Сектор A", [seat(1, 1, 5000), seat(1, 2, 5000)]))
    seats = extract_seats(hp)
    assert len(find_runs(seats, max_price=15000, seats_needed=2)) == 1
    assert find_runs(seats, max_price=15000, seats_needed=3) == []


def test_find_runs_does_not_join_rows_or_levels():
    hp = hallplan(
        level("Сектор A", [seat(1, 5, 5000), seat(2, 6, 5000)]),
        level("Сектор B", [seat(1, 6, 5000)]),
    )
    assert find_runs(extract_seats(hp), max_price=15000, seats_needed=2) == []


FAKE_EVENT_HTML = """
<html><head>
<title>Билеты на «Баста» в БСА «Лужники» — концерты в Москве на Яндекс Афише</title>
</head><body>
{"Ticket:Mjk2Nnw3MzIzNTd8MzI5MjE0N3wxNzg4MDE5MjAwMDAw":{"saleStatus":"available"},
 "Ticket:Mjk2Nnw3MzIzNTd8MzI5MjE0N3wxNzg4MTA1NjAwMDAw":{"saleStatus":"available"},
 "hash":"c29tZS1yYW5kb20tbm9uc2Vuc2U0Mg=="}
</body></html>
"""


def test_extract_sessions_and_pick():
    sessions = extract_sessions(FAKE_EVENT_HTML)
    # два сеанса (29 и 30 августа), случайный base64-мусор отброшен
    assert len(sessions) == 2
    key29 = "Mjk2Nnw3MzIzNTd8MzI5MjE0N3wxNzg4MDE5MjAwMDAw"
    dt, event_id = sessions[key29]
    assert (dt.strftime("%Y-%m-%d %H:%M"), event_id) == ("2026-08-29 19:00", "732357")

    picked_key, picked_dt = pick_session(sessions, "2026-08-29")
    assert picked_key == key29 and picked_dt == dt

    with pytest.raises(ValueError):  # две даты без SESSION_DATE — неоднозначно
        pick_session(sessions)
    with pytest.raises(ValueError):  # нет сеанса на эту дату
        pick_session(sessions, "2026-08-31")
    with pytest.raises(ValueError):  # пустая страница
        pick_session({})


def test_extract_event_name():
    assert extract_event_name(FAKE_EVENT_HTML) == "«Баста» в БСА «Лужники»"
    assert extract_event_name("<html></html>") == "событие"


def test_parse_sectors():
    assert parse_sectors("") is None
    assert parse_sectors("C134-C139,A109-A112") == [("C", 134, 139), ("A", 109, 112)]
    assert parse_sectors("A110") == [("A", 110, 110)]
    assert parse_sectors("С134-С139") == [("C", 134, 139)]  # кириллическая С
    with pytest.raises(ValueError):
        parse_sectors("ерунда")
    with pytest.raises(ValueError):
        parse_sectors("C134-A139")  # разные буквы в диапазоне


def test_sector_matches():
    allowed = parse_sectors("C134-C139,A109-A112")
    assert sector_matches("Сектор C134", allowed)
    assert sector_matches("Сектор A110 (VIP)", allowed)
    assert not sector_matches("Сектор C141 (ограниченная видимость)", allowed)
    assert not sector_matches("Сектор D118", allowed)
    assert not sector_matches("Танцпол", allowed)
    assert sector_matches("Танцпол", None)  # без фильтра подходит всё


def test_extract_seats_sector_filter():
    hp = hallplan(
        level("Сектор C134", [seat(1, 1, 6000), seat(1, 2, 6000)]),
        level("Сектор D118", [seat(5, 3, 8000), seat(5, 4, 8000)]),
    )
    seats = extract_seats(hp, allowed_sectors=parse_sectors("C134-C139,A109-A112"))
    assert {s["level"] for s in seats} == {"Сектор C134"}


def test_handle_command_help_and_unknown():
    import basta_watcher
    cfg = {"max_price": 15000, "seats_needed": 2, "ignore_limited_view": True,
           "target_url": "https://example.com"}
    assert "/check" in basta_watcher.handle_command(cfg, "/start")
    assert basta_watcher.handle_command(cfg, "/help@SomeBot") == basta_watcher.HELP_TEXT
    assert basta_watcher.handle_command(cfg, "просто текст") is None


def test_find_runs_sorted_by_price_desc_then_numeric_row():
    hp = hallplan(
        level("Сектор A", [seat(10, 1, 7000), seat(10, 2, 7000),
                           seat(2, 1, 7000), seat(2, 2, 7000)]),
        level("Сектор B", [seat(1, 1, 3500), seat(1, 2, 3500)]),
    )
    runs = find_runs(extract_seats(hp), max_price=15000, seats_needed=2)
    # дорогие варианты первыми, внутри одной цены — ряды по возрастанию
    assert [(r["level"], r["row"]) for r in runs] == [
        ("Сектор A", "2"), ("Сектор A", "10"), ("Сектор B", "1"),
    ]


def test_buy_link_picks_most_expensive_window():
    hp = hallplan(level("Сектор A", [
        seat(3, 1, 5000), seat(3, 2, 5000), seat(3, 3, 9000), seat(3, 4, 9000),
    ], level_id=62))
    runs = find_runs(extract_seats(hp), max_price=15000, seats_needed=2)
    assert len(runs) == 1
    url = buy_link("KEY", runs[0], seats_needed=2)
    parsed = urlparse(url)
    assert parsed.path == "/w/sessions/KEY"
    import json
    payload = json.loads(unquote(parse_qs(parsed.query)["selectedSeats"][0]))
    # из цепочки 1-4 взято самое дорогое окно: места 3 и 4 по 9000
    assert payload == [
        {"level": 62, "row": "3", "place": "3"},
        {"level": 62, "row": "3", "place": "4"},
    ]
