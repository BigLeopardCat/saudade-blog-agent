"""A complete small time series must not become an inaccessible seven-day excerpt."""

from datetime import date, timedelta

from agent.reports import render_note_stats


def series(count):
    return [{"date": (date(2026, 9, 1) + timedelta(days=index)).isoformat(),
             "views": index + 1, "likes": 0, "favorites": 0} for index in range(count)]


def test_all_daily_values_in_the_thirty_day_contract_reach_the_model():
    rows = series(26)
    text = render_note_stats({"daily": rows})
    for row in rows:
        assert f"{row['date']} 阅读 {row['views']}" in text
    assert "更早还有" not in text


def test_unexpected_oversized_series_remains_bounded_and_declares_the_omission():
    rows = series(40)
    text = render_note_stats({"daily": rows})
    assert rows[0]["date"] not in text
    assert all(row["date"] in text for row in rows[-30:])
    assert "更早还有 10 天" in text
