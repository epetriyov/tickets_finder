"""Тесты чистой логики: разбор hallplan и поиск цепочек соседних мест.

Синтетический hallplan повторяет реальную структуру ответа
widget.afisha.yandex.ru (см. шапку afisha_api.py) — если API изменится
и extract_seats() придётся править, эти тесты зафиксируют контракт.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afisha_api import extract_seats, find_runs


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


def level(name, seats, admission=False):
    return {"name": name, "admission": admission, "seats": seats}


def test_extract_seats_basic():
    hp = hallplan(level("Сектор B105", [seat(23, 5, 12000), seat(23, 6, 12000)]))
    seats = extract_seats(hp)
    assert len(seats) == 2
    assert seats[0] == {
        "level": "Сектор B105", "row": "23", "place": 5,
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


def test_handle_command_help_and_unknown():
    import basta_watcher
    cfg = {"max_price": 15000, "seats_needed": 2, "ignore_limited_view": True,
           "target_url": "https://example.com"}
    assert "/check" in basta_watcher.handle_command(cfg, "/start")
    assert basta_watcher.handle_command(cfg, "/help@SomeBot") == basta_watcher.HELP_TEXT
    assert basta_watcher.handle_command(cfg, "просто текст") is None


def test_find_runs_sorted_by_price_then_numeric_row():
    hp = hallplan(
        level("Сектор A", [seat(10, 1, 7000), seat(10, 2, 7000),
                           seat(2, 1, 7000), seat(2, 2, 7000)]),
        level("Сектор B", [seat(1, 1, 3500), seat(1, 2, 3500)]),
    )
    runs = find_runs(extract_seats(hp), max_price=15000, seats_needed=2)
    assert [(r["level"], r["row"]) for r in runs] == [
        ("Сектор B", "1"), ("Сектор A", "2"), ("Сектор A", "10"),
    ]
