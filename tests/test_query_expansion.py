"""查询侧同义扩展（`rag/search.py::_QUERY_SYNONYMS`）的离线自测。
"""


from __future__ import annotations

import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


import rag.search as rs  # noqa: E402

FAILED: list[str] = []
check = collector(FAILED)


_FIXTURE = [
    {"type": "note", "id": 1, "title": "设备接入物联网平台指南",
     "content": "## 鉴权\n设备用 JWT 换取访问权限，JWT 由平台签发，有效期两小时。\n"
                "## 连接\nMQTT over TLS，证书链要做校验。"},
    {"type": "note", "id": 2, "title": "看板娘 agent 架构文档",
     "content": "## 中断与收尾\n客户端一断开就触发断连中断，本轮不再继续执行。\n"
                "## 记忆\n对话历史落在数据库里。"},
    {"type": "note", "id": 3, "title": "ESP32-S3 OTA 问题与解决记录",
     "content": "## 分区\nOTA 需要两个 app 分区，partition 表冲突会导致回滚。\n"
                "## 上传\n走本地 HTTP 上传固件。"},
    {"type": "note", "id": 4, "title": "Git 从入门到入土",
     "content": "## 分支\n分支只是一个指针，所以很轻量。\n## 快照\ncommit 是一份快照。"},
    {"type": "note", "id": 5, "title": "评测体系说明",
     "content": "## 评测\n评测分两层，确定性层判零红，能力层报区间。"},
]


def _build_fixture_index() -> rs.RagIndex:
    """离线索引：短路语料拉取 + 摘掉向量预热（后者会联网）。"""
    rs.warm_vectors = lambda chunks: None  # type: ignore[assignment]
    ix = rs.RagIndex()
    ix._fetch_corpus = lambda: [dict(d) for d in _FIXTURE]  # type: ignore[method-assign]
    ix.build()
    return ix


def _hits(query: str) -> list[str]:
    return [f"{h['type']}:{h['id']}" for h in (rs.search(query, top_k=5) or [])]


def _rank(hits: list[str], doc: str) -> int | None:
    return next((i + 1 for i, h in enumerate(hits) if h == doc), None)


def test_query_expansion():
    _CASES: list[tuple[str, str, str]] = [
        ("测评", "测评怎么样？", "note:5"),
        ("令牌", "令牌怎么样？", "note:1"),
        ("断线", "断线怎么样？", "note:2"),
        ("空中升级", "空中升级怎么样？", "note:3"),
    ]
    _NEUTRAL = "分支为什么很轻量？"
    ix = _build_fixture_index()
    rs._index = ix  # get_index() 的进程级单例指向夹具
    rs.set_expansion(False)
    _OFF = {q: _hits(q) for _, q, _ in _CASES}
    _OFF[_NEUTRAL] = _hits(_NEUTRAL)
    rs.set_expansion(True)
    _ON = {q: _hits(q) for _, q, _ in _CASES}
    _ON[_NEUTRAL] = _hits(_NEUTRAL)
    PAIRS = rs.synonyms()
    print("① 词表结构")
    check("表非空", bool(PAIRS), f"{len(PAIRS)} 对")
    check("每对都有 key / val / why 三个键，且都非空",
          all(p.get("key") and p.get("val") and (p.get("why") or "").strip() for p in PAIRS))
    check("key ≠ val", all(p["key"] != p["val"] for p in PAIRS))
    check("key / val 长度都 ≥ 2（单字做替换会命中一切）",
          all(len(p["key"]) >= 2 and len(p["val"]) >= 2 for p in PAIRS))
    check("key 不重复（重复的那条会被前一条盖住，静默失效）",
          len({p["key"] for p in PAIRS}) == len(PAIRS))
    print("\n② 死词闸门：每对词都必须被标注集里某条 query 真的用到")
    try:
        import recall_eval as re  # noqa: E402
        _LABELED = [q["query"] for q in re.QUERIES] + [q["query"] for q in re.HOLDOUT]
    except Exception as e:  # 标注集读不到 ⇒ 红，不是跳过
        _LABELED = []
        check("标注集可读（主集 + 留出集）", False, str(e))
    for p in PAIRS:
        hit = [q for q in _LABELED if p["key"] in q]
        check(f"「{p['key']}」被标注集用到（{len(hit)} 条）", bool(hit),
              "；".join(hit[:2]) or "**没人用 ⇒ 死词**")
    print("\n③ 正控：夹具上每一对都真的改变过结果（词表↔现场双向同步）")
    check("词表每对都在 _CASES 里有现场", {p["key"] for p in PAIRS} == {k for k, _, _ in _CASES},
          f"表={sorted(p['key'] for p in PAIRS)} 现场={sorted(k for k, _, _ in _CASES)}")
    for key, q, doc in _CASES:
        off, on = _OFF[q], _ON[q]
        check(f"「{key}」：{q} 关臂 {off or '空'} → 开臂 {on}，且 {doc} 排第一",
              off != on and _rank(on, doc) == 1,
              f"关臂 rank={_rank(off, doc)} / 开臂 rank={_rank(on, doc)}")
    print("\n④ 负控 A：不含任何 key 的 query 两臂一字不差（扩展开关只在命中 key 时生效）")
    check(f"{_NEUTRAL} 两臂相同", _OFF[_NEUTRAL] == _ON[_NEUTRAL], str(_ON[_NEUTRAL]))
    _OTHER = _hits("commit 是一份快照吗？")
    rs.set_expansion(False)
    check("换一条不含 key 的 query 复验", _OTHER == _hits("commit 是一份快照吗？"), str(_OTHER))
    rs.set_expansion(True)
    print("\n⑤ 负控 B：val 换成语料里不存在的词 ⇒ 结果回到关臂（扩展不凭替换动作本身造噪声）")
    _SAVED = rs._QUERY_SYNONYMS
    rs._QUERY_SYNONYMS = tuple(
        {**p, "val": "太空梯"} if p["key"] == "令牌" else p for p in PAIRS)
    for key, q, _doc in _CASES:
        if key != "令牌":
            continue
        check(f"「{key}」的 val 不在语料里 ⇒ 与关臂一致（空）",
              _hits(q) == _OFF[q] == [], str(_hits(q)))
    rs._QUERY_SYNONYMS = _SAVED
    print("\n⑥ 开关契约：关掉 == 关臂，且 set_expansion 是唯一入口")
    check("set_expansion(False) 后 expansion_enabled() 为 False",
          (rs.set_expansion(False), rs.expansion_enabled() == False)[1])  # noqa: E712
    check("关臂可复现：再关一次结果与上面那次逐字节相同",
          all(_hits(q) == _OFF[q] for _, q, _ in _CASES))
    rs.set_expansion(True)
    check("打开后 expansion_enabled() 为 True 且结果回到开臂",
          rs.expansion_enabled() and all(_hits(q) == _ON[q] for _, q, _ in _CASES))
    print("\n⑦ 幂等：key 与 val 同时在 query 里 ⇒ 与关臂一致（同一批 gram 不重复计权重）")
    _DUP = "令牌和 JWT 分别怎么校验？"
    rs.set_expansion(False)
    _dup_off = _hits(_DUP)
    rs.set_expansion(True)
    check("「令牌和 JWT…」两臂相同（替换出的 gram 与原文去重）",
          _hits(_DUP) == _dup_off, f"{_dup_off} vs {_hits(_DUP)}")
    rs._index = None
    rs.set_expansion(True)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_query_expansion()
