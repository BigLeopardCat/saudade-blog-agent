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
    assert '2026-11-01 阅读 0' in text and '点赞 0' in text


def test_zero_dates_and_nonview_metrics_survive_a_mixed_daily_series():
    rows = [
        {'date': '2026-11-01', 'views': 0, 'likes': 0, 'favorites': 0, 'comments': 0},
        {'date': '2026-11-02', 'views': 3, 'likes': 0, 'favorites': 0, 'comments': 0},
        {'date': '2026-11-03', 'views': 0, 'likes': 0, 'favorites': 1, 'comments': 2},
    ]
    text = render_note_stats({'daily': rows})
    assert all(row['date'] in text for row in rows)
    assert '2026-11-01 阅读 0' in text and '2026-11-03 阅读 0' in text
    assert '收藏 1' in text and '讨论 2' in text
    assert '只列有记录' not in text


def test_missing_counts_remain_unknown_inside_a_measured_series():
    text = render_note_stats({'daily': [
        {'date': '2026-11-01', 'views': 0, 'likes': 0}, {'date': '2026-11-02'}]})
    assert '2026-11-02 阅读 0' not in text
    assert '未返回日期和指标保持未知' in text


def test_zero_days_reach_the_model_through_the_actual_admin_tool(monkeypatch):
    import httpx
    from _framework_fixture import call, model_replies
    from _own_support import config
    from langchain_core.messages import HumanMessage

    from agent.principal import SOURCE_ASSERTION, Principal
    from agent.runtime.session import ToolSession
    from tools import base
    from tools.admin_read import get_note_stats

    rows = series(30)
    rows[0].update(views=0, likes=0, favorites=0, comments=0)
    reads = []

    def handle(request):
        reads.append(request)
        return httpx.Response(200, json={'code': 200, 'data': {'daily': rows}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, '_client', client)
        with model_replies({'tool_calls': [call('get_note_stats', 'stats')]},
                           {'content': '首日访问为零。'}) as (model, requests):
            cfg = config(principal=Principal(7, 'admin', SOURCE_ASSERTION))
            result = ToolSession(model, cfg, {'get_note_stats': get_note_stats}, {}).run(
                [HumanMessage(content='读取逐日访问趋势')])
    message = next(m for m in requests[-1]['messages'] if m['role'] == 'tool')
    assert f"{rows[0]['date']} 阅读 0" in message['content']
    assert all(row['date'] in message['content'] for row in rows)
    assert len(reads) == 1 and result.reply == '首日访问为零。'


def test_lifetime_rankings_and_recent_daily_values_keep_distinct_scopes():
    text = render_note_stats({
        'totalViews': 1200, 'totalLikes': 7, 'totalFavorites': 3,
        'topViewed': [{'noteId': 23, 'title': '跨年文章', 'views': 900,
                       'likes': 2, 'favorites': 1}],
        'topLiked': [], 'topFavorited': [],
        'daily': [{'date': '2026-11-03', 'views': 4, 'likes': 0}],
        'dailyWindow': {'scope': 'rolling_window', 'start': '2026-10-05',
                        'end': '2026-11-03',
                        'monthlySubtotals': [{'month': '2026-11', 'views': 4}]},
    })
    assert '历史累计合计' in text and '阅读 1200' in text
    ranking = text.split('历史累计阅读榜', 1)[1].split('历史累计点赞榜', 1)[0]
    assert 'noteId 23' in ranking and '阅读 900' in ranking
    assert '窗口内2026-11小计（不是整月合计）：阅读 4' in text
    assert '2026-11-03 阅读 4' in text
    assert '本期阅读榜' not in text


def test_user_report_keeps_unlisted_users_and_missing_counts_unknown():
    from agent.reports import render_user_stats

    text = render_user_stats({"totalUsers": 19, "listedUsers": 9,
                              "users": [{"id": 7, "name": "示例", "role": "user"}]})
    assert "本次明细 1 人" in text
    assert "未列出的用户活动未知" in text and "会话 未知／消息 未知" in text
    empty = render_user_stats({"totalUsers": 19, "users": []})
    assert "没有任何用户" not in empty and "不能据此判断全站活动" in empty
