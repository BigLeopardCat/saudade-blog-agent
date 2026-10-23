from eval.request_metrics import summary


def test_first_visible_and_reply_metrics_keep_missing_values_missing():
    doc = {
        "input": {"source": "test", "runtime": {"arm": "hybrid"}},
        "events": [
            {"node": "producer", "event": "first_visible_event",
             "kind": "progress", "duration_s": 0.25},
            {"node": "producer", "event": "first_reply", "duration_s": 1.5},
        ],
        "duration_s": 2.0,
        "reply": "ok",
    }
    row = summary(doc)
    assert row["first_visible_event_s"] == 0.25
    assert row["first_reply_s"] == 1.5
    assert summary({"events": []})["first_reply_s"] is None


def test_metrics_reject_invalid_and_unrelated_observations(monkeypatch):
    from eval.request_metrics import collect

    doc = {"events": [
        {"node": "tool", "event": "first_reply", "duration_s": 0},
        *({"node": "producer", "event": "first_reply", "duration_s": value}
          for value in (True, -1, float("nan"), float("inf"), "1")),
        {"node": "producer", "event": "first_reply", "duration_s": 2},
        {"node": "producer", "event": "first_reply", "duration_s": 3},
    ]}
    monkeypatch.setattr("eval.request_metrics.load_trace",
                        lambda path: doc if path == "new" else {})
    group = collect(["new", "old"])["groups"][0]
    assert group["first_reply_samples"] == 1
    assert group["first_reply_p50_s"] == 2
    assert group["first_visible_samples"] == 0
    assert group["first_visible_p50_s"] is None
