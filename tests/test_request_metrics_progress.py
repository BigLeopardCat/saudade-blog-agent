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
