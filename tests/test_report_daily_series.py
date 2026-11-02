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


def test_report_uses_backend_coverage_instead_of_its_own_clock_or_daily_rows():
    text = render_note_stats({'generatedAt': '2030-01-01', 'daily': [], 'dailyWindow': {
        'scope': 'rolling_window', 'start': '2026-10-05', 'end': '2026-11-03',
        'monthlySubtotals': [{'month': '2026-10', 'views': 167},
                             {'month': '2026-11', 'views': 4}]}})
    assert '2026-10-05 至 2026-11-03' in text
    assert '窗口内2026-10小计（不是整月合计）：阅读 167' in text
    assert '窗口内2026-11小计（不是整月合计）：阅读 4' in text


def test_missing_coverage_and_subtotals_remain_unknown_without_agent_aggregation():
    text = render_note_stats({'daily': series(30), 'generatedAt': '2030-01-01'})
    assert '后端未返回覆盖范围' in text
    assert '本次日序列范围' not in text and '小计' not in text


def test_monthly_trend_accepts_real_daily_or_period_observations_but_not_failed_reads():
    import json
    from pathlib import Path

    from eval import native_assertions

    cases = Path(__file__).resolve().parents[1] / 'eval/golden/native_requests.jsonl'
    case = next(row for row in map(json.loads, cases.read_text().splitlines())
                if row['id'] == 'admin_periods')
    gold = {'require_observations': case['gold']['require_observations']}
    for name, kind, valid in (('get_note_stats', 'ok', True),
                              ('get_note_periods', 'ok', True),
                              ('get_note_stats', 'unavailable', False),
                              ('get_service_health', 'ok', False)):
        result = {'native_events': [{'node': 'native', 'event': 'call',
                                     'name': name, 'kind': kind}]}
        assert (not native_assertions.check(gold, result)) is valid


def test_missing_daily_rows_and_missing_metrics_are_not_zero_observations():
    for rows in ([], [{'date': '2026-11-01'}]):
        text = render_note_stats({'daily': rows})
        assert '不能据此断言零访问' in text
    text = render_note_stats({'daily': [{'date': '2026-11-01', 'views': 0, 'likes': 0}]})
    assert '返回日期内的阅读和点赞均为零' in text
