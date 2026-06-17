"""Tests for the pure tally logic in car_usage.py.

These don't touch the network or OAuth — they exercise count_cars() / helpers
against fixture result dicts shaped like the iRacing Data API `result` endpoint.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from car_usage import class_short_names, count_cars  # noqa: E402


def _race_session(rows):
    """Wrap result rows in a race simsession + a non-race one to be ignored."""
    return {
        "session_results": [
            {"simsession_type_name": "Qualify", "simsession_type": 3, "results": [
                {"car_name": "Should Ignore", "car_class_short_name": "IMSA23"},
            ]},
            {"simsession_type_name": "Race", "simsession_type": 5, "results": rows},
        ]
    }


def test_counts_only_target_class_and_ranks_by_entries():
    results = [
        _race_session([
            {"car_name": "Ferrari 296 GT3", "car_class_short_name": "IMSA23"},
            {"car_name": "Ferrari 296 GT3", "car_class_short_name": "IMSA23"},
            {"car_name": "BMW M4 GT3", "car_class_short_name": "IMSA23"},
            {"car_name": "Porsche 963 GTP", "car_class_short_name": "IMSAP"},  # other class
        ]),
        _race_session([
            {"car_name": "Ferrari 296 GT3", "car_class_short_name": "IMSA23"},
            {"car_name": "BMW M4 GT3", "car_class_short_name": "IMSA23"},
        ]),
    ]

    counts = count_cars(results, "IMSA23")

    # Ferrari (3) > BMW (2); GTP car excluded; qualify session ignored.
    assert counts.most_common()[0] == ("Ferrari 296 GT3", 3)
    assert counts["BMW M4 GT3"] == 2
    assert "Porsche 963 GTP" not in counts
    assert sum(counts.values()) == 5


def test_class_matching_is_case_insensitive():
    results = [_race_session([
        {"car_name": "Ferrari 296 GT3", "car_class_short_name": "IMSA23"},
    ])]
    assert count_cars(results, "imsa23")["Ferrari 296 GT3"] == 1


def test_falls_back_to_car_class_name_field():
    results = [_race_session([
        {"car_name": "Audi R8 LMS GT3", "car_class_name": "IMSA23"},
    ])]
    assert count_cars(results, "IMSA23")["Audi R8 LMS GT3"] == 1


def test_unknown_class_returns_empty_and_hints_list_available():
    results = [_race_session([
        {"car_name": "Ferrari 296 GT3", "car_class_short_name": "IMSA23"},
    ])]
    assert count_cars(results, "NOPE") == {}
    hints = class_short_names(results)
    assert "IMSA23" in hints


def test_missing_car_name_falls_back_to_car_id():
    results = [_race_session([
        {"car_id": 173, "car_class_short_name": "IMSA23"},
    ])]
    assert count_cars(results, "IMSA23")["car_id:173"] == 1


if __name__ == "__main__":
    # Allow running without pytest installed.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
