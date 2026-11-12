"""Public answer coordinates are checked; unusable annotations do not remove questions."""

from eval.prepare_cmrc_spans import convert


def test_offset_recovery_is_unambiguous_and_every_question_is_retained():
    context = "## 标题\n甲是答案。甲还是答案。唯一实体。"
    raw = {"data": [{"id": "article", "title": "title", "paragraphs": [{
        "id": "paragraph", "context": context, "qas": [
            {"id": "valid", "question": "实体是什么？", "answers": [
                {"text": "唯一实体", "answer_start": context.index("唯一实体")}]},
            {"id": "recover", "question": "实体是什么？", "answers": [
                {"text": "唯一实体", "answer_start": 0}]},
            {"id": "ambiguous", "question": "哪个甲？", "answers": [
                {"text": "甲", "answer_start": 0}]},
            {"id": "heading", "question": "标题？", "answers": [
                {"text": "标题", "answer_start": 3}]},
        ]}]}]}
    corpus, queries, qrels, audit = convert(raw)
    assert corpus["article:paragraph"]["text"] == context
    assert set(queries) == set(qrels) == {"valid", "recover", "ambiguous", "heading"}
    assert queries["recover"]["evidence_spans"] == queries["valid"]["evidence_spans"]
    assert "evidence_spans" not in queries["ambiguous"]
    assert "evidence_spans" not in queries["heading"]
    assert audit["span_queries"] == 2
    assert audit["queries_without_representable_span"] == ["ambiguous", "heading"]
    assert audit["annotations"]["unique_quote_offset_recovered"] == 1
