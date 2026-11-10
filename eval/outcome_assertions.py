"""Assert structured tool, confirmation, receipt and source-document outcomes."""

import json
import re

from agent import confirm
from eval import corpus_terms
from eval.native_assertions import forbidden_call_failures

FORBID_TOKENS = ("@write_console",)


def write_console_tools() -> set:
    """当前全集的 `write.console` 工具（唯一事实源 = `agent.authz.TOOL_SCOPE`）。"""
    from agent.authz import SCOPE_WRITE_CONSOLE, TOOL_SCOPE

    return {t for t, s in TOOL_SCOPE.items() if s == SCOPE_WRITE_CONSOLE}


def expand_token_list(lst: list) -> list:
    """Expand scope sentinels to the current tool set.

    Raise on empty or unreadable scopes so a write prohibition cannot silently
    become an empty restriction. All scope-based assertions share this expansion."""
    if not any(t in FORBID_TOKENS for t in lst):
        return list(lst)
    names = sorted(write_console_tools())
    if not names:
        raise RuntimeError("工具名清单里的哨兵展开了空集（authz.TOOL_SCOPE 变了？）")
    out: list = []
    for t in lst:
        out.extend(names if t in FORBID_TOKENS else [t])
    return out


def squash_ws(s: str) -> str:
    """Normalize layout whitespace without changing source punctuation."""
    return re.sub(r"\s+", "", s.replace("　", " "))


def _exec_claim_fails(gold: dict, result: dict) -> list[str]:
    """Check receipt membership and per-tool execution counts independently.

    require_exec_tools requires every listed tool. require_exec_once_any requires
    at least one candidate and exactly one receipt for each executed candidate.
    Do not use the latter when repeated calls for different targets are intended."""
    out: list[str] = []
    for t in gold.get("require_exec_tools", []):
        if t not in result["exec_tools"]:
            out.append(f"执行回执缺少工具 {t}（exec：{result['exec_tools']}）")
    _once = gold.get("require_exec_once_any") or []
    if not _once:
        return out
    _ran = [t for t in _once if t in result["exec_tools"]]
    _n = {
        t: sum(1 for r in result["exec_rows"] if isinstance(r, dict) and r.get("tool") == t)
        for t in _ran
    }
    if not _ran:
        out.append(
            f"这些件里一件都没真执行（这一格不是空转的）{_once}（exec：{result['exec_tools']}）"
        )
        return out
    _bad = {t: v for t, v in _n.items() if v > 1}
    if _bad:
        _all = [r.get("tool") for r in result["exec_rows"] if isinstance(r, dict)]
        out.append(
            f"同一件在一轮里被兑现了不止一次 {_bad}（一次点头 = 一件办一次）；本轮全部回执={_all}"
        )
    return out


def check_tools(gold, result, *, docs=None, user_input="", pending_src=""):
    fails = []
    for t in gold.get("require_tool_calls", []):
        if t not in result["tool_calls"]:
            fails.append(f"未调用工具 {t}（已调用：{result['tool_calls']}）")
    # Allow any declared supplier tool when the model may choose the read strategy.
    _any_of = gold.get("require_tool_calls_any") or []
    if _any_of and not any(x in result["tool_calls"] for x in _any_of):
        fails.append(f"未调用任一检索工具 {_any_of}（已调用：{result['tool_calls']}）")

    fails += forbidden_call_failures(
        gold, result, expand_token_list(gold.get("forbid_tool_calls") or [])
    )
    # Check receipt presence and counts independently.
    fails += _exec_claim_fails(gold, result)
    # Preparation rounds must prove zero backend mutations, not merely omit named tools.
    if gold.get("require_zero_exec"):
        _wtools = set(expand_token_list(["@write_console"]))  # 展开成空集会抛，同 forbid 侧
        _wcalls = [t for t in result["tool_calls"] if t in _wtools]
        _wreceipts = [
            r.get("tool")
            for r in result["exec_rows"]
            if isinstance(r, dict) and r.get("tool") in _wtools
        ]
        if _wcalls or _wreceipts:
            fails.append(
                "本轮应零写执行（后台写工具一个都不许调用、也不许有验收回执），实际："
                f"写工具调用={_wcalls}，写回执={_wreceipts}；"
                f"（本轮全部调用={result['tool_calls']}，"
                f"全部回执={[r.get('tool') for r in result['exec_rows'] if isinstance(r, dict)]}）"
            )
    # Forbidden execution tools must not appear in factual receipts.
    for t in gold.get("forbid_exec_tools", []):
        if t in result["exec_tools"]:
            fails.append(f"不应有工具 {t} 的执行回执（exec：{result['exec_tools']}）")

    # Unresolved parameter references cannot count as successful execution arguments.
    for r in result["exec_rows"]:
        for k, v in (r.get("args") or {}).items():
            if re.match(r"^\$[a-z_]+\[\d+\]", str(v)):
                fails.append(f"执行回执里出现未解析的引用参数 {r.get('tool')}.{k}={v!r}")

    # Consumer arguments must originate from the declared producer result.
    for spec in gold.get("require_arg_from_result", []):
        producers = spec.get("producers") or [spec["producer"]]
        prods = [r for r in result["exec_rows"] if r.get("tool") in producers]
        cons = [r for r in result["exec_rows"] if r.get("tool") == spec["consumer"]]
        if not prods or not cons:
            fails.append(
                f"依赖链断言缺回执：{'/'.join(producers)}×{len(prods)} / "
                f"{spec['consumer']}×{len(cons)}"
            )
            continue
        fields = spec.get("fields") or [spec.get("field") or "id"]
        pool: set = set()
        for p in prods:
            txt = str(p.get("result") or "")
            for f in fields:
                pool |= set(re.findall(rf"{f}\D{{0,4}}(\d+)", txt))
        got = str((cons[-1].get("args") or {}).get(spec["arg"]) or "")
        if got not in pool:
            # Distinguish an empty producer result from missing evidence that a producer ran.
            if not pool:
                hint = (
                    f"（回执里抠不出任何 {'/'.join(fields)} 形式的 id——**要么生产者真没"
                    f"返回、要么字段名不认识、要么回执被截断到 200 字**；回执原文前 120 字："
                    f"{str(prods[-1].get('result') or '')[:120]!r}）"
                )
            else:
                hint = f"（回执里只有 {sorted(pool)}）"
            fails.append(
                f"{spec['consumer']}.{spec['arg']}={got!r} 不来自 "
                f"{'/'.join(producers)} 的 {'/'.join(fields)}{hint}"
                f" —— 取的 id 不是检索结果给的"
            )

    # A terminal fallback cannot count as an answer when the case forbids it.
    for p in gold.get("require_frame_prefix", []):
        if not any(f.startswith(p) for f in result["frames"]):
            fails.append(f"本轮没有发出 {p} 帧（控制帧：{[f[:24] for f in result['frames']]}）")
    for p in gold.get("forbid_frame_prefix", []):
        hits = [f for f in result["frames"] if f.startswith(p)]
        if hits:
            fails.append(f"本轮不应发出 {p} 帧（实际发了一条：{hits[0][:60]}）")

    return fails


def check_confirmation(gold, result, *, docs=None, user_input="", pending_src=""):
    text = result["text"]
    fails = []
    _cp = gold.get("require_confirm_payload")
    if _cp:
        _pays = [p for p in (result.get("confirm_payloads") or []) if p]
        if not _pays:
            fails.append("本轮没有可读的确认载荷（没弹卡，或控制帧里没带 token）")
        else:
            _pay = _pays[0]
            _specs = _pay.get("specs") or []
            if _cp.get("workflow") and _pay.get("workflow") != _cp["workflow"]:
                fails.append(f"卡片技能不符：期望 {_cp['workflow']}，载荷 {_pay.get('workflow')!r}")
            # A workflow family accepts the model-selected member among the declared alternatives.
            if _cp.get("workflow_any") and _pay.get("workflow") not in _cp["workflow_any"]:
                fails.append(
                    f"卡片技能不在期望族里：期望 {_cp['workflow_any']}，"
                    f"载荷 {_pay.get('workflow')!r}"
                )
            if "specs" in _cp and len(_specs) != _cp["specs"]:
                fails.append(f"卡片参数条数不符：期望 {_cp['specs']}，载荷 {len(_specs)}")
            # Check the number of independently selectable proposal units.
            if "units" in _cp:
                _u = confirm.units_of(_pay)
                if _u is None:
                    fails.append("卡片载荷里的单元分组读不懂（老令牌没有分组时按逐条一单元算）")
                elif len(_u) != _cp["units"]:
                    fails.append(
                        f"卡片单元数不符：期望 {_cp['units']} 格，载荷 {len(_u)} 格（分组 {_u}）"
                    )
            # Require the exact declared operation to be included in the signed proposal.
            for _want in _cp.get("specs_include") or []:
                _wt = str((_want or {}).get("tool") or "")
                _wa = (_want or {}).get("args") or {}
                _hit = [
                    s
                    for s in _specs
                    if str(s.get("tool") or "") == _wt
                    and all(str((s.get("args") or {}).get(k)) == str(v) for k, v in _wa.items())
                ]
                if not _hit:
                    _got = [{k: s.get(k) for k in ("tool", "args")} for s in _specs]
                    fails.append(
                        "卡片上没有这一件："
                        f"{json.dumps(_want, ensure_ascii=False)}（载荷里是 "
                        f"{json.dumps(_got, ensure_ascii=False)}）"
                    )
            # Verify parameter-body provenance from the supplied request and pending fixture.
            for _f in _cp.get("args_from_input") or []:
                _vals = [str((s.get("args") or {}).get(_f) or "") for s in _specs]
                _src = squash_ws(user_input)
                _led = squash_ws(pending_src)
                if not _src and not _led:
                    # Missing provenance evidence is unevaluated, never a pass.
                    fails.append(
                        f"卡片参数 {_f!r} 判不了：这一轮既拿不到主人原话、"
                        "也拿不到台账那一行"
                        "（`check_gold` 的 `user_input` / `pending_src` 都传空了）"
                    )
                elif not _vals or not all(
                    v and (squash_ws(v) in _src or (bool(_led) and squash_ws(v) in _led))
                    for v in _vals
                ):
                    fails.append(
                        f"卡片参数 {_f!r} 对不回主人这句话、也对不回台账那一行："
                        f"载荷 {_vals!r}，原话 {user_input[:40]!r}，"
                        f"台账 {pending_src[:40]!r}"
                    )
        for _tk in result.get("confirm_tokens") or []:
            if _tk and _tk in text:
                fails.append("令牌原文出现在正文里（它是 10 分钟有效的写授权凭据）")

    return fails


def check_execution_results(gold, result, *, docs=None, user_input="", pending_src=""):
    fails = []
    for spec in gold.get("require_exec_args", []):
        rows = [r for r in result["exec_rows"] if r.get("tool") == spec["tool"]]
        if not rows:
            fails.append(f"缺少 {spec['tool']} 的执行回执（无法核对参数）")
            continue
        want = str(spec["equals"])
        got = [str((r.get("args") or {}).get(spec["arg"]) or "") for r in rows]
        if want not in got:
            fails.append(f"{spec['tool']}.{spec['arg']} 期望 {want}，实际 {got}")

    # Check receipt prose with declared positive and negative patterns.
    for spec in gold.get("require_exec_result", []):
        _rows = [r for r in result["exec_rows"] if r.get("tool") == spec.get("tool")]
        if not _rows:
            fails.append(f"缺少 {spec.get('tool')} 的执行回执（无法核对回执文本）")
            continue
        _blob = "\n".join(str(r.get("result") or "") for r in _rows)
        _hit, _miss = spec.get("match"), spec.get("not_match")
        if not _hit and not _miss:
            fails.append(f"require_exec_result 没声明 match/not_match（这条什么都没判）：{spec!r}")
            continue
        if _hit and not re.search(_hit, _blob):
            fails.append(f"{spec['tool']} 的回执文本里没有 {_hit!r}（回执：{_blob[:120]}）")
        if _miss and re.search(_miss, _blob):
            fails.append(
                f"{spec['tool']} 的回执文本命中了不该出现的 {_miss!r}"
                f"—— 那一次**没有真的发生变更**（后端走了 no-op 分支），"
                f"回执：{_blob[:120]}"
            )

    return fails


def check_documents(gold, result, *, docs=None, user_input="", pending_src=""):
    text = result["text"]
    fails = []
    for spec in gold.get("require_doc_terms", []):
        if docs is None:
            fails.append(
                "[未评估] 本轮没有语料快照，require_doc_terms 判不了"
                "（未评估 ≠ 通过；跑法需在启动时取一次快照逐例传入）"
            )
            continue
        terms, tdiag = corpus_terms.derive(
            spec.get("doc") or [],
            docs=docs,
            df_max=int(spec.get("df_max", 2)),
            strict=bool(spec.get("strict", False)),
            cap=None,  # 判据侧不截断：cap 只服务回显，截断会把用了常见词的诚实回答判红
        )
        if tdiag.get("unavailable"):
            fails.append(f"[未评估] 语料不可用（{tdiag.get('why') or '快照为空'}）")
            continue
        if tdiag.get("no_doc") or tdiag.get("missing"):
            fails.append(
                "申报的文档不在语料里（"
                f"{tdiag.get('missing') or '用例没写 require_doc_terms.doc'}）"
                f"—— 期望过期，请更新该用例的 `doc`（不是模型退化）"
            )
            continue
        hits = corpus_terms.hit_terms(terms, text)
        want = int(spec.get("min_terms", 2))
        if len(hits) < want:
            fails.append(
                f"回复没扎根在申报的 {'、'.join(tdiag['docs'])} 里：命中 {len(hits)}/{want} 个"
                f"该篇派生术语（派生集 {tdiag.get('n_kept')} 个；"
                f"该篇常用词如 {tdiag.get('common')}）"
            )

    return fails
