"""Signed batch scope, actual final tags, cancellation and replay are separate oracles."""

from agent import confirm
from agent.domains.article_tags import batch
from eval.article_tag_fixture import backend

REQUEST = "给文章11和12都加上「测试」标签，其他标签保留。准备一张整批确认卡。"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def prepare(turn, state):
    result = turn(REQUEST)
    require(len(result["confirm_tokens"]) == 1 and not state["writes"],
            "batch did not produce exactly one read-only approval")
    token = result["confirm_tokens"][0]
    grant = confirm.verify(token, 7, 42)
    args = batch.combine(grant["specs"])
    require(grant["executor"] == "article_tags"
            and [row["id"] for row in args["items"]] == [11, 12]
            and args["intent"]["add"] == ["测试"] and args["intent"]["replace"] is None,
            "approved article set or exact tag name changed")
    require(sum(r.method == "POST" and r.url.path.endswith("/prepare")
                for r in state["requests"]) == 1,
            "explicit batch required mechanical article-by-article preparations")
    return token


def complete(turn, state):
    token = prepare(turn, state)
    done = turn("", token)
    require(not done["confirm_tokens"] and done["text"].strip(), "batch did not finish")
    require(state["writes"] == [[11, 12]] and len(state["tags"]) == 1,
            "batch wrote unexpected targets or created duplicate tags")
    require(all([tag["name"] for tag in state["rows"][key]["tags"]] == ["测试"]
                for key in (11, 12)) and not state["rows"][13]["tags"],
            "actual final tag state does not match the approved scope")
    require(any(row["tool"] == batch.NAME for row in done["exec_rows"]), "missing batch receipt")
    replay = turn("", token)
    require(state["writes"] == [[11, 12]] and not replay["confirm_tokens"],
            "batch replay repeated a write or prepared another card")


def replace_scope(turn, state):
    old = prepare(turn, state)
    replacement = turn("改一下，只给文章12加「演示」标签，文章11不要动。替换刚才的确认卡。")
    require(len(replacement["confirm_tokens"]) == 1 and not state["writes"],
            "changed request did not replace the old approval without writes")
    token = replacement["confirm_tokens"][0]
    spec, = confirm.verify(token, 7, 42)["specs"]
    args = spec["args"]
    ids = ([row["id"] for row in args["items"]]
           if spec["tool"] == batch.NAME else [args["article_id"]])
    require(ids == [12] and args["intent"]["add"] == ["演示"], "replacement scope changed")
    turn("", old)
    require(not state["writes"], "revoked batch executed")
    done = turn("", token)
    require(not done["confirm_tokens"] and state["writes"] == [[12]]
            and not state["rows"][11]["tags"], "replaced batch changed an excluded article")


CASES = [("article_tags_batch", backend, complete),
         ("article_tags_batch_replace", backend, replace_scope)]
