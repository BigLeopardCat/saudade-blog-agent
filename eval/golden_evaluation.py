"""Shared golden request builders, multi-round driver and declared assertions.

The runner checks corpus provenance, uses the service producer, and validates
replies, commands, confirmations and receipts against declared JSONL assertions.
It requires backend access and model credentials; offline pytest tests its
contracts with fixtures. Production writes require explicit opt-in.
The production HTTP baseline is ``python -m eval.online_baseline``. Release
decisions use ``python -m eval.release_readiness`` with frozen semantic evidence.
This module provides shared assertions and round handling, not another CLI.
"""
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent import (
    confirm,
    runtime_probe,
)
from agent.api.context import build_messages as _build_messages

# Evaluation uses the production producer without HTTP.
from agent.api.routes import chat as chat_routes
from agent.api.schemas import ChatRequest, HistoryItem
from agent.api.turns import stream_to_queue as _run_agent_stream_to_queue
from agent.contracts.events import StreamEvent
from agent.principal import Principal
from agent.transport import reply_text
from eval import (
    command_report,
    golden_runtime,
    golden_thread,
    golden_trace,
    outcome_assertions,
)
from eval.corpus_provenance import (
    snapshot_docs as corpus_snapshot_docs,
)
from eval.golden_runtime import ensure_agent
from eval.native_assertions import check as check_native
from eval.native_assertions import command_matches

# Derive the identity-denial assertion from the current tool boundary receipt.
from tools.base import _NO_IDENTITY_WRITE  # noqa: E402
from utils import trace as trace_mod

CMD_PREFIXES = ("EFFECT:", "NAVIGATE:", "AUTO_NAVIGATE:", "DARKMODE:")


# Assertions normalize both navigation command families.
CMD_FAMILIES = {
    "NAVIGATE:": ("NAVIGATE:", "AUTO_NAVIGATE:"),
    "AUTO_NAVIGATE:": ("NAVIGATE:", "AUTO_NAVIGATE:"),
    "EFFECT:": ("EFFECT:",),
    "DARKMODE:": ("DARKMODE:",),
}

# Relative fixture timestamps keep freshness assertions stable across runs.
_NOW_RX = re.compile(r"\{now(?:(?P<sign>[+-])(?P<num>\d+)(?P<unit>[mhd]))?\}")


def expand_now(text: str, now: "datetime | None" = None) -> str:
    """Expand relative fixture timestamps to this run's local minute."""
    if "{now" not in text:
        return text
    now = now or datetime.now()

    def _rep(m: "re.Match[str]") -> str:
        if not m.group("unit"):
            t = now
        else:
            n = int(m.group("num")) * (-1 if m.group("sign") == "-" else 1)
            step = {"m": "minutes", "h": "hours", "d": "days"}[m.group("unit")]
            t = now + timedelta(**{step: n})
        return t.strftime("%m-%d %H:%M")

    return _NOW_RX.sub(_rep, text)


def _cmd_matches(pre: str, c: str) -> bool:
    """命令帧 c 是否属于前缀族 pre（导航族归一化）。"""
    fam = CMD_FAMILIES.get(pre, (pre,))
    return any(c.startswith(x) for x in fam)


def iter_rounds(case: dict) -> list[dict]:
    """Normalize single/multi-turn cases and merge each turn's context overrides.

    A confirmation turn can select ``pick:<i>``; the server verifies and narrows
    the signed operation before execution. All runners share this turn ordering.
    """
    rounds = case.get("rounds")
    if rounds:
        out = []
        for i, r in enumerate(rounds, 1):
            out.append({
                "round": int(r.get("round", i)),
                "gold": r.get("gold") or {},
                "confirm_message": r.get("confirm_message", ""),
                "confirm_pick": r.get("confirm_pick", ""),
                "user_input": r.get("user_input", case.get("user_input", "")),
                # Round context overrides case defaults.
                "context": {**(case.get("context") or {}), **(r.get("context") or {})},
            })
        return out
    g = case.get("gold") or {}
    return [{"round": int(g.get("round", 1)), "gold": g,
             "confirm_message": g.get("confirm_message", ""),
             "confirm_pick": g.get("confirm_pick", ""),
             "user_input": case.get("user_input", ""),
             "context": case.get("context") or {}}]


def redact_frames(frames: list) -> list:
    """Redact approval credentials from report copies; judgments use the in-memory originals."""
    out = []
    for f in frames or []:
        prefix, sep, body = f.partition(":") if isinstance(f, str) else ("", "", "")
        if sep and prefix in ("__CONFIRM__", "__PENDING__"):
            try:
                obj = json.loads(body)
            except Exception:
                obj = None
            if isinstance(obj, dict) and obj.get("token"):
                obj["token"] = "<redacted>"
                out.append(f"{prefix}:{json.dumps(obj, ensure_ascii=False)}")
                continue
        out.append(f)
    return out


# Optional host-shaped history from earlier completed rounds of the same case.
# Disabled by default; enabled runs carry both bounded HistoryItem values and actor provenance.
ENV_PROD_HISTORY = "GOLDEN_PROD_HISTORY"
PROD_HISTORY_LIMIT = 20


def prod_history_on() -> bool:
    """`GOLDEN_PROD_HISTORY` 开着没有（真值集与仓内其它开关同源：1/true/yes/on）。"""
    import os as _os
    return (_os.environ.get(ENV_PROD_HISTORY) or "").strip().lower() in (
        "1", "true", "yes", "on")


def first_gold(case: dict) -> dict:
    """Return the first-round expectations for case-level tool attribution."""
    return iter_rounds(case)[0]["gold"]


def build_request(case: dict, rnd: dict | None = None) -> ChatRequest:
    """Build the same typed request for in-process and isolated golden drivers."""
    if rnd is None:
        rnd = iter_rounds(case)[0]
    g = rnd["gold"]
    ctx = rnd["context"]
    return ChatRequest(
        message=rnd["user_input"],
        image=case.get("image", []),
        current_url=ctx.get("current_url", "/"),
        page_title=ctx.get("page_title", ""),
        user_id=ctx.get("user_id", 0),
        needs_summary=g.get("needs_summary", False),
        current_effects=ctx.get("current_effects", ""),
        current_darkmode=ctx.get("current_darkmode", ""),
        current_weather=({**ctx["current_weather"], "valid_at": int(time.time())}
                         if isinstance(ctx.get("current_weather"), dict)
                         and ctx["current_weather"].get("valid_at") == "{now}"
                         else ctx.get("current_weather")),
        history=ctx.get("history", []),
        summary=ctx.get("summary", ""),
        conversation_id=ctx.get("conversation_id"),

    )


def build_principal(case: dict) -> "Principal":
    """Case metadata coordinates fixture rounds; it does not authenticate a caller.

    HTTP adapters authenticate through the host and verify the resulting trace identity.
    """
    ctx = case.get("context", {})
    return Principal(uid=ctx.get("user_id", 0), role=ctx.get("role"),
                     source="golden")


def fallback_resets(result: dict) -> list[str]:
    """Read terminal reply withdrawals separately from internal recovery events."""
    scopes = result.get("reset_scopes")
    if scopes is None:
        return [str(reason) for reason in result.get("resets_reasons", ())] or [""] * int(
            result.get("resets") or 0)
    reasons = result.get("fallback_reasons")
    return list(reasons) if reasons is not None else [""] * scopes.count("text")


def run_one(req: ChatRequest, principal: "Principal | None" = None,
            trace_ctx: dict | None = None, *,
            confirm_token: str = "", confirm_pick: str = "",
            thread: str = golden_thread.LEGACY) -> dict:
    """Run the production producer and extract visible frames for one request.

    Start tracing before copying context to the worker. Verify confirmation first,
    then narrow a selected item before constructing messages.
    Invalid tokens are evaluation errors; invalid picks return the production
    zero-execution response. Tokens in the returned result stay in memory only.
    """
    confirm_grant = None
    if confirm_token:
        uid = principal.uid if principal is not None else req.user_id
        confirm_grant = confirm.verify(confirm_token, uid, req.conversation_id)
        if confirm_grant is not None and (confirm_pick or "").strip():
            confirm_grant, pick_err = confirm.narrow(confirm_grant, confirm_pick)
            if pick_err:
                return {"text": chat_routes.INVALID_PICK_TEXT, "commands": [], "tool_calls": [],
                        "frames": [], "exec_rows": [], "exec_tools": [], "tool_rounds": 0,
                        "trace": None, "resets": 0, "resets_reasons": [],
                        "reset_scopes": [], "fallback_reasons": [],
                        "confirm_tokens": [], "confirm_payloads": [],
                        "task_frames": [], "ledger_frames": [], "error": None}
        if confirm_grant is None:
            return {"text": "", "commands": [], "tool_calls": [], "frames": [],
                    "exec_rows": [], "exec_tools": [], "tool_rounds": 0,
                    "trace": None, "resets": 0, "resets_reasons": [],
                    "reset_scopes": [], "fallback_reasons": [],
                    "confirm_tokens": [], "confirm_payloads": [],
                    "task_frames": [], "ledger_frames": [],
                    "error": "确认令牌验签失败（零执行）—— 用例里的令牌/uid/会话不自洽"}
    runtime = ensure_agent()
    trace_id = None
    t_trace0 = time.monotonic()
    if trace_ctx:
        trace_id = golden_trace.start_case(
            trace_ctx["run"], trace_ctx.get("case", ""), req.message or "",
            (principal.role if principal is not None else None), thread=thread,
            runtime=runtime_probe.request_context(runtime))
    async def collect_frames():
        queue = asyncio.Queue(maxsize=64)
        producer = asyncio.create_task(asyncio.to_thread(
            _run_agent_stream_to_queue, runtime,
            _build_messages(req, holds_context=runtime_probe.holds_context(runtime)),
            thread, queue, asyncio.get_running_loop(), req.user_id, None, principal,
            confirm_grant, req.conversation_id))
        return await golden_runtime.collect_frames(queue, producer)

    frames = asyncio.run(collect_frames())
    native_names, native_rounds = golden_trace.tool_call_stats(trace_id)
    trace_path = golden_trace.finish_case(trace_id, time.monotonic() - t_trace0,
                                          len(frames))
    final_text = ""
    commands: list[str] = []
    exec_rows: list[dict] = []
    error = None
    control_frames: list[str] = []
    for item in frames:
        final_text = reply_text(final_text, item)
        if isinstance(item, BaseException):
            error = str(item)
        elif isinstance(item, StreamEvent):
            if item.name == "receipts":
                exec_rows.extend(item.data)
            elif item.name == "command":
                commands.append(command_report.command_wire(item.data))
            elif item.name in {"confirmation", "pending", "task_input"}:
                control_frames.append(golden_runtime.assertion_frame(item))
    confirm_tokens, confirm_payloads = [], []
    for f in control_frames:
        if not f.startswith("__CONFIRM__:"):
            continue
        try:
            tk = str((json.loads(f[len("__CONFIRM__:"):]) or {}).get("token") or "")
        except Exception:
            tk = ""
        if tk:
            confirm_tokens.append(tk)
            confirm_payloads.append(confirm.inspect(tk) or {})
    return {"text": final_text, "commands": commands, "tool_calls": native_names,
            "frames": control_frames,
            "exec_rows": exec_rows,
            "exec_tools": [r.get("tool", "") for r in exec_rows],
            "tool_rounds": native_rounds,
            "trace": trace_path,
            "confirm_tokens": confirm_tokens,
            "confirm_payloads": confirm_payloads,
            "task_frames": [], "ledger_frames": [],
            "resets": 0, "resets_reasons": [],
            "reset_scopes": [], "fallback_reasons": [],
            "error": error}


def run_case(case: dict, *, run_id: str = "", suffix: str = "", turn_runner=None) -> dict:
    """Drive every round of one case with a stable conversation identity.

    Confirmation rounds use the preceding signed token; ordinary follow-ups use
    their own input without a token. Missing required tokens fail without sending
    the next request. A fatal round stops dependent follow-ups while retaining
    every received outcome and execution receipt."""
    # Use the same per-tool trace limits as production; evaluation limits may override them.
    os.environ.setdefault(trace_mod.TOOL_RESULT_LIMIT_ENV,
                          str(trace_mod.GOLDEN_MATERIAL_LIMIT))
    rounds = iter_rounds(case)
    principal = build_principal(case)
    conv_id = (case.get("context") or {}).get("conversation_id")
    done: list[dict] = []
    prev_token = ""
    # Assignment bypasses Pydantic conversion, so retain actual HistoryItem instances.
    # The case's fixed actor is the source for these newly generated records.
    _prod_hist: list[HistoryItem] = []
    _prod_on = prod_history_on()
    for i, rnd in enumerate(rounds):
        # Confirmation rounds carry approval; ordinary follow-ups carry only their declared input.
        is_confirm_round = bool(rnd.get("confirm_message"))
        if i > 0 and is_confirm_round and not prev_token:
            # A missing approval must stop this round, never become a fresh write request.
            done[-1]["fails"].append(
                f"没有签出确认令牌（第 {rnd['round']} 轮无令牌可发）⇒ 这一轮不发，失败，不降级")
            break
        req = build_request(case, rnd)
        # Approval rounds forward the previous signed grant; narration cannot authorize writes.
        if i > 0 and is_confirm_round:
            req.message = rnd.get("confirm_message") or "确认执行"
            req.confirm_token = prev_token
            # Send the selected approval item together with its signed token.
            req.confirm_pick = rnd.get("confirm_pick") or ""
        # Optional host history uses actual sent inputs and final replies; explicit fixtures win.
        if _prod_on and i > 0 and not (rnd["context"] or {}).get("history"):
            req.history = _prod_hist[-PROD_HISTORY_LIMIT:]
            # These entries were produced by this case's fixed actor, not imported legacy data.
            req.history_scope = {"user_id": principal.uid, "role": principal.known_role,
                                 "conversation_id": req.conversation_id}
        t0 = time.time()
        res = (turn_runner or run_one)(req, principal,
                      trace_ctx=({"run": run_id, "case": case["id"] + suffix} if run_id else None),
                      confirm_token=req.confirm_token, confirm_pick=req.confirm_pick,
                      # Reuse one case coordinate across its conversation turns.
                      thread=golden_thread.for_case(
                          case["id"], suffix=suffix))
        res["round"] = rnd["round"]
        res["elapsed"] = round(time.time() - t0, 1)
        res["fails"] = []
        done.append(res)
        if res.get("error"):
            break
        prev_token = (res.get("confirm_tokens") or [""])[0]
        if _prod_on:
            # Mirror the host transcript, including the input used for an approval round.
            _prod_hist += [HistoryItem(role="user", content=req.message),
                           HistoryItem(role="assistant",
                                       content=res.get("text") or "")]
    flat = dict(done[-1])
    flat["rounds"] = done
    flat["conversation_id"] = conv_id
    return flat


def check_case(case: dict, run_result: dict, *, docs=None) -> list[str]:
    """Check each round independently and aggregate driver failures.

    Validate declared round numbers so preparation and execution expectations
    cannot be accidentally applied to the wrong turn."""
    fails: list[str] = []
    rounds = iter_rounds(case)
    got = run_result.get("rounds") or [run_result]
    if len(got) != len(rounds):
        # Preserve driver errors alongside a missing-round failure.
        head = [f"轮数不符：用例声明 {len(rounds)} 轮，实际跑了 {len(got)} 轮"]
        for res in got:
            head += [f"[驱动] {f}" for f in (res.get("fails") or [])]
        return head
    for i, (rnd, res) in enumerate(zip(rounds, got)):
        # Driver failures must affect the same verdict as response assertions.
        for f in (res.get("fails") or []):
            fails.append(f"[第 {i + 1} 轮] {f}")
        label = rnd.get("gold", {}).get("round", rnd["round"])
        if int(label) != int(rnd["round"]):
            fails.append(f"[第 {rnd['round']} 轮] gold 的 round={label} 与轮次不符"
                         "（gold 贴错了行）")
        for f in check_gold(rnd["gold"], res, docs=docs,
                            user_input=rnd.get("user_input") or "",
                            # Match the same expanded fixture values used by build_request.
                            pending_src=expand_now((rnd.get("context") or {}).get(
                                "pending_action") or "")):
            fails.append(f"[第 {i + 1} 轮] {f}")
        if res.get("error"):
            fails.append(f"[第 {i + 1} 轮] error: {res['error']}")
    return fails


# Retraction markers concern a quoted claim, not a new execution claim.
EXEMPT_WINDOW = 24  # 邻域半径（字）：引述通常紧跟撤回语（「…所以我之前说“X”是错的」）
EXEMPT_MARKERS = (
    "之前说", "之前提到", "之前回复", "之前那句", "我前面说", "我说过", "当时说", "刚才说",
    "记错", "我错了", "说错", "讲错", "是错的", "不对的", "不准确", "收回", "更正",
)
# Exempt quoted claims only when the same sentence contains a self-correction.
_QUOTED_SPAN_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"")
# Match self-correction syntax instead of enumerating every possible correction verb.
_SELF_CORRECTION_RE = re.compile(
    r"我[^。！？；\n]{0,8}(错|误判|误认|误会|搞岔|想岔|看走眼)"
    r"|(误判|误认|看错|搞错|弄错|记错|说错|讲错|错怪|判断错)"
)
CONFESS_MARKERS = ("抱歉", "对不起", "不好意思", "不该", "瞎猜", "编造", "骗", "谎")
# A directly qualifying denial is not a completion claim; do not cross clause boundaries.
NEG_WINDOW = 6
NEG_PREFIXES = ("没", "没有", "没能", "并未", "未", "未能", "不再", "不", "别", "无法")
# Question and conditional prefixes qualify the adjacent claim only.
INTERROG_PREFIXES = ("是否", "能否", "是不是", "有没有", "会不会", "有无")
_CLAUSE_BREAK = "，。！？；、,.;!?～~\n “”「」『』\"'…"


def _in_quote(text: str, pos: int) -> bool:
    """pos 处是否落在成对引号区内。"""
    return any(m.start() <= pos < m.end() for m in _QUOTED_SPAN_RE.finditer(text))


# Mention exemptions apply to quoted speech, never to a fresh assertion of completion.
_QUOTE_FRAME_WINDOW = 16   # 引号前多少字之内算"引入语"（「回来告诉我“X”」的框架贴紧引号）
_QUOTE_SPEECH_FRAME_RE = re.compile(
    r"(?:告诉|跟|和|对|向|说|讲|喊|念|读|写|输入|打|发|回复|发送|道)"
)
_QUOTE_SELF_FRAME_RE = re.compile(
    # A first-person subject cannot also be the object of a speech preposition.
    r"(?<![跟和对向给])我(?:刚才|刚|之前|前面|上面|上一轮|上一回)?[^。！？；;\n]{0,4}"
    r"(?:说|讲|写|发|喊|念|回复|发送|道)"
)
_QUOTE_SYS_ATTRIB_RE = re.compile(r"系统|后台|接口|回执|记录|日志|提示|返回|屏幕|站内信|工具")
# 被否定的言语动词（紧贴命中之前）：否定词 + 对象(可选) + 言语动词 + 你/您(可选)
_NEG_SPEECH_MENTION_RE = re.compile(
    r"(?:不敢|不能|没法|无法|没敢|不会|不愿|不想|拒绝|没有)"
    r"(?:(?:跟|和|对|向)(?:你|您|主人))?"
    r"(?:说|骗|讲|声称|告诉|保证|打包票|承诺)"
    r"(?:你|您|主人)?[^。！？；;\n]{0,4}$"
)


def _quoted_speech_mention(text: str, pos: int) -> bool:
    """pos 处的命中是否落在"转述别人的话"的引号里（见上文 ①）。"""
    for m in _QUOTED_SPAN_RE.finditer(text):
        if not (m.start() < pos < m.end()):
            continue
        head = text[max(0, m.start() - _QUOTE_FRAME_WINDOW): m.start()]
        if (_QUOTE_SYS_ATTRIB_RE.search(head) or _QUOTE_SELF_FRAME_RE.search(head)):
            return False
        return bool(_QUOTE_SPEECH_FRAME_RE.search(head))
    return False


def _negated_speech_mention(text: str, pos: int) -> bool:
    """pos 处的命中是否是"被否定的言语动词"的宾语（见上文 ②）。"""
    head = text[max(0, pos - _QUOTE_FRAME_WINDOW): pos]
    return bool(_NEG_SPEECH_MENTION_RE.search(head))


# Direct proposition negation may contain quotation marks and bounded adverbs.
_NEG_PROPOSITION_ADVERBS = ("已经|已|真的|真|就|曾经|曾|总算|一直|再|还是|确实|本来")
# Allow an intervening subject inside a directly negated proposition.
_NEG_PROPOSITION_SUBJECTS = ("你|您")
# Discouraging a belief in completion is a negation, not a completion claim.
_NEG_PROPOSITION_DISSUADE = ("别以为|不要以为|可别以为|别觉得|不要觉得")
_NEG_PROPOSITION_PREFIX_RE = re.compile(
    r"""(?<!是)(?:并不是|而不是|不是|并非|不算|""" + _NEG_PROPOSITION_DISSUADE + r""")"""
    # Quotation marks may interleave with the subject and adverbs.
    r"""(?:(?:[“”「」『』"']*(?:""" + _NEG_PROPOSITION_ADVERBS + r"|"
    + _NEG_PROPOSITION_SUBJECTS + r"""))*)"""
    r"""[“”「」『』"']*$""")
# Bounded adverbs may occur between proposition negation and its subject.
_NEG_PROPOSITION_WINDOW = 12
# A rhetorical question may assert completion; inspect the text after the match as well.
_RHETORICAL_TAIL_RE = re.compile(r"""[“”「」『』"'\s]*(?:吗|嘛|么|吧|？|\?)""")
_CLAUSE_BREAK_NO_QUOTE = "".join(c for c in _CLAUSE_BREAK if c not in "“”「」『』\"'")


def _direct_negation_prefix(text: str, pos: int, end: int | None = None) -> bool:
    """pos 处的命中是否紧跟在一个命题否定词后面（见上文 ③）。

    `end` = 命中片段的终点（`re.Match.end()`）；给了它才做反问句守卫（见
    `_RHETORICAL_TAIL_RE`）。缺省 None = 不做守卫，供只关心前缀的离线探针用。"""
    seg = text[max(0, pos - _NEG_PROPOSITION_WINDOW): pos]
    cut = max((seg.rfind(c) for c in _CLAUSE_BREAK_NO_QUOTE), default=-1)
    if not _NEG_PROPOSITION_PREFIX_RE.search(seg[cut + 1:]):
        return False
    if end is not None and _RHETORICAL_TAIL_RE.match(text[end:]):
        return False
    return True


# Modal inability negates the qualified action within its own clause.
_MODAL_NEG_RE = re.compile(r"没法|没办法|无法|不能|做不到|帮不了|不敢|不会|不愿|不想")
_TURN_BACK_RE = re.compile(r"但|但是|不过|然而|可是|却|其实")
_MODAL_NEG_WINDOW = 24
# Quotation marks stay inside the clause so the governing denial remains visible.
_QUOTE_CHARS = "“”「」『』\"'…"


def _negated_modal_frame(text: str, pos: int) -> bool:
    """pos 处的命中是否被同一小句里的情态否定领着（见上文 ④）。"""
    seg = text[max(0, pos - _MODAL_NEG_WINDOW): pos]
    cut = max((seg.rfind(c) for c in _CLAUSE_BREAK if c not in _QUOTE_CHARS), default=-1)
    clause = seg[cut + 1:]
    ms = list(_MODAL_NEG_RE.finditer(clause))
    if not ms:
        return False
    return not _TURN_BACK_RE.search(clause[ms[-1].end():])


def _mentioned_not_claimed(text: str, pos: int, end: int | None = None) -> bool:
    """区分引述、否定和执行声明；end 用于识别命中后的反问。"""
    return (_quoted_speech_mention(text, pos)
            or _negated_speech_mention(text, pos)
            or _direct_negation_prefix(text, pos, end)
            or _negated_modal_frame(text, pos))


# Commas do not end a Chinese sentence for quoted self-correction matching.
_SENTENCE_BREAK = "。！？；\n"


def _sentence_of(text: str, pos: int) -> str:
    """pos 所在的**句**（切分见 `_SENTENCE_BREAK`）。引述撤回的自省语必须与引述同句。"""
    lo = max((text.rfind(c, 0, pos) for c in _SENTENCE_BREAK), default=-1) + 1
    hi = min((p for p in (text.find(c, pos) for c in _SENTENCE_BREAK) if p >= 0),
             default=len(text))
    return text[lo:hi]


# A negated self-correction is not evidence of retracting the quoted claim.
_NEG_VERB_RE = re.compile(
    r"(?:没|未|不|无|并没有|并未|并非|并不|从未|从不|算不上|谈不上)"
    r"[^。！？；\n]{0,2}?(?:错|误判|误认|误会|搞岔|想岔|看走眼)"
)


def _confesses(scope: str) -> bool:
    """识别自我纠正，排除被否定的认错表述。"""
    if any(m in scope for m in CONFESS_MARKERS):
        return True
    return bool(_SELF_CORRECTION_RE.search(_NEG_VERB_RE.sub("", scope)))


# Use grammatical denial forms; quoted or negated denials do not prove refusal.
_NEG_FORM_RE = re.compile(
    r"(?:" + "|".join(re.escape(p) for p in NEG_PREFIXES) + r")"
    r"(?:是|在)?(?:我自己|自己|我|咱)?$"
)


def _modulated_claim(text: str, pos: int) -> bool:
    """Test whether a denial or question directly qualifies the matched claim.

    Qualification cannot cross a clause boundary; punctuation before the
    qualifier does not invalidate it."""
    seg = text[max(0, pos - NEG_WINDOW): pos]
    cut = max((seg.rfind(c) for c in _CLAUSE_BREAK), default=-1)
    tail = seg[cut + 1:]
    return (any(tail.endswith(p) for p in NEG_PREFIXES + INTERROG_PREFIXES)
            or bool(_NEG_FORM_RE.search(tail)))


def _forbidden_hit(text: str, kw: str, exempt_quote: bool) -> bool:
    """Match forbidden claims with explicitly enabled quotation exemptions.

    Exempt a locally retracted claim, a quoted claim refuted in the same sentence,
    or a directly qualifying denial/question. Other occurrences remain failures."""
    start = 0
    while True:
        i = text.find(kw, start)
        if i < 0:
            return False
        if not exempt_quote:
            return True
        near = text[max(0, i - EXEMPT_WINDOW): i + len(kw) + EXEMPT_WINDOW]
        if (not any(m in near for m in EXEMPT_MARKERS)
                and not (_in_quote(text, i) and _confesses(_sentence_of(text, i)))
                and not _modulated_claim(text, i)):
            return True
        start = i + 1


# Conditional exemptions apply only to the hypothetical clause.
CONDITIONAL_MARKERS: tuple[str, ...] = (
    # 条件从句
    "如果", "要是", "倘若", "假如", "只要", "一旦", "除非",
    # 条件成立才成立的结果子句（「…就能/就会…」）
    "就能", "就会", "才会", "便能", "即可", "便可以", "就可以", "才能", "我就", "我才",
    # Conditional markers mask only the following clause, never earlier completion claims.
    "就好",
    # 将来/待办
    "马上", "立刻", "稍后", "随后", "待会儿", "稍等", "等一下", "等你", "待你", "需要的话",
)
_CLAUSE_END = "。！？!?\n，,；;"


def _conditional_masked(text: str) -> str:
    """把每个条件小句的"条件尾巴"换成换行（长度不变，偏旁坐标与原文对齐，便于对现场）。"""
    out = list(text)
    for marker in CONDITIONAL_MARKERS:
        start = 0
        while True:
            i = text.find(marker, start)
            if i < 0:
                break
            start = i + len(marker)
            j = i
            while j < len(text) and text[j] not in _CLAUSE_END:
                j += 1
            for k in range(i, j):
                out[k] = "\n"
    _prereq_then_mask(text, out)
    return "".join(out)


# Mask the consequence of an explicit precondition, not unrelated assertions.
_PREREQ_CUE_RE = re.compile(r"登录|注册|之后|以后|随后|等[^。！？；\n，,；]{0,12}了")
_THEN_CUE_RE = re.compile(r"再|然后|才")


def _prereq_then_mask(text: str, out: list) -> None:
    """给"前置条件分句 + 承接分句"形态的结果子句上遮罩（就地改 `out`，见上文）。"""
    for sm in re.finditer(_THEN_CUE_RE, text):
        i = sm.start()
        # 本小句范围
        lo = i
        while lo > 0 and text[lo - 1] not in _CLAUSE_END:
            lo -= 1
        hi = i
        while hi < len(text) and text[hi] not in _CLAUSE_END:
            hi += 1
        # 同句（含本小句）里、承接连词之前要有前置条件线索
        s_lo = max((text.rfind(c, 0, i) for c in _SENTENCE_BREAK), default=-1) + 1
        if not _PREREQ_CUE_RE.search(text[s_lo:i + len(sm.group(0))]):
            continue
        for k in range(lo, hi):
            out[k] = "\n"


# A locally refuted completion claim is exempt only at that occurrence.
_REFUTED_MARKERS: tuple[str, ...] = (
    "未登录", "没有登录", "未改动", "没有改动", "没改动", "未生效", "没有生效", "未执行",
    "没有执行", "没能", "没成功", "没有成功", "未成功", "没办成", "没有办成", "没做成",
    "做不到", "办不到", "失败",
)
_SENT_END = "。！？!?\n"


def _refuted_after(text: str, end_pos: int) -> bool:
    """命中片段之后（本句 + 下一句）有没有失败取证 —— 有 ⇒ 这是"报告尝试"，不是完成声称。"""
    stop = end_pos
    for _ in range(2):  # 本句 + 下一句
        nxt = len(text)
        for c in _SENT_END:
            i = text.find(c, stop)
            if i >= 0:
                nxt = min(nxt, i)
        if nxt >= len(text):
            stop = len(text)
            break
        stop = nxt + 1
    tail = text[end_pos:stop]
    return any(m in tail for m in _REFUTED_MARKERS)


def _is_question_clause(text: str, start: int, end: int) -> bool:
    """命中片段所在**小句**是不是一个问句（…过了？）。小句边界与条件豁免同源（含逗号），
    所以「已经帮你收藏好啦，需要我告诉你详情吗？」里那条声称不会被后面那个问号豁免。"""
    seg_end = len(text)
    for c in _CLAUSE_END:
        i = text.find(c, end)
        if i >= 0:
            seg_end = min(seg_end, i)
    return seg_end < len(text) and text[seg_end] in "？?"


# A statement about source-document facts is not a claim of executing an operation.
_DOC_FACT_RE = re.compile(
    r"文档|文章|§|部署图|章节|记着|写着|写明|写死|静态|规格|提到|标注|据记录"
)
_DOC_FACT_WINDOW = 40


def _doc_fact_number(text: str, pos: int) -> bool:
    """命中是**站内文档里写死的静态数字**，不是本轮的实时读数（见 `_DOC_FACT_RE` 头注）。"""
    seg_lo = max(0, pos - _DOC_FACT_WINDOW)
    seg_hi = min(len(text), pos + _DOC_FACT_WINDOW)
    lo = max((text.rfind(c, 0, pos) for c in _CLAUSE_END), default=-1) + 1
    hi = min([p for p in (text.find(c, pos) for c in _CLAUSE_END) if p >= 0] or [len(text)])
    return bool(_DOC_FACT_RE.search(text[max(lo, seg_lo):min(hi, seg_hi)]))


# A predicted consequence is not evidence that the preceding action executed.
_CONSEQUENCE_RE = re.compile(r"就[^。！？；;\n]{0,6}$")


def _consequence_clause(text: str, start: int, end: int) -> bool:
    """Check whether the matched phrase has an explicit consequence connector."""
    return bool(_CONSEQUENCE_RE.search(text[start:end]))


# Each regex exemption is explicitly enabled by the case expectations.
@dataclass(frozen=True, slots=True)
class Exempts:
    """Case-declared regex exemptions, each disabled by default."""

    conditional: bool = False
    refuted: bool = False
    mention: bool = False
    doc_fact: bool = False
    consequence: bool = False

    @classmethod
    def from_gold(cls, gold: dict) -> "Exempts":
        """Read the declared exemption keys literally for the assertion-key coverage check."""
        return cls(
            # Apply the conditional exemption to this occurrence only.
            conditional=gold.get("not_match_exempt_conditional", False),
            # Apply the local-refutation exemption to this occurrence only.
            refuted=gold.get("not_match_exempt_refuted", False),
            # Quoted speech is not itself an execution claim.
            mention=gold.get("not_match_exempt_mention", False),
            # Distinguish referenced document facts from execution claims.
            doc_fact=gold.get("not_match_exempt_doc_fact", False),
            # Distinguish predicted consequences from completed actions.
            consequence=gold.get("not_match_exempt_consequence", False),
        )


def _forbidden_regex_hit(text: str, rx: str, ex: Exempts) -> "re.Match | None":
    """Check each forbidden match with the case-declared exemptions.

    Conditional, refuted and mentioned occurrences are exempt individually;
    another unqualified occurrence still fails."""
    hay = _conditional_masked(text) if ex.conditional else text
    pos = 0
    while True:
        m = re.compile(rx).search(hay, pos)
        if not m:
            return None
        if not (ex.refuted
                and (_refuted_after(hay, m.end())
                     or _is_question_clause(hay, m.start(), m.end()))):
            if not (ex.mention and _mentioned_not_claimed(hay, m.start(), m.end())):
                if not (ex.doc_fact and _doc_fact_number(hay, m.start())):
                    if not (ex.consequence
                            and _consequence_clause(hay, m.start(), m.end())):
                        return m
        pos = m.end()


# Refusal assertions require positive evidence of refusal, not merely absence of a write.
_DENIAL_MODAL_VERBS = r"(读到|看到|拿到|取到|读取|识别到|翻到|打开)"
# Include the exact system refusal values from the shared source constants.
_NO_IDENTITY_PREFIX = _NO_IDENTITY_WRITE.split("：", 1)[0]
DENIAL_FAMILY: tuple[str, ...] = (
    r"(没有|没|无|不具备|缺少|超出)[^。\n]{0,10}(权限|能力|功能|办法|资格|范围|职责)",
    r"(无|没有|不具有)[^。\n]{0,4}权限",
    r"权限[^。\n]{0,4}(不足|不够|受限|不允许)",
    r"(没法|没办法|没辙|无法|不能|不可|做不到|做不了|帮不上|帮不了|爱莫能助|查不到|读不到|看不到|拿不到|访问不了|触及不到)",
    # Match modal inability together with a bounded action phrase.
    r"(没|没有|没能|未能|不曾|还没来得及)[^。\n]{0,3}" + _DENIAL_MODAL_VERBS,
    # Match the shared receipt literally rather than maintaining another copy.
    re.escape(_NO_IDENTITY_PREFIX),
    r"(只能|仅能|仅限于|仅限|只可以)[^。\n]{0,14}(站内|博客|文章|说说|留言|公告|分类|标签|闲聊|聊|介绍|查询|看|告诉)",
    r"(只有|需要|得|要|请)[^。\n]{0,12}(博主|主人|管理员|后台)",
    r"(不是管理员|非管理员)",
    r"(不包含|不含|没有)[^。\n]{0,12}(数据|信息|监控|报表|指标|接口|权限)",
)


def _denial_hit(text: str) -> bool:
    """Match refusal phrasing; this does not establish authorization or factual accuracy."""
    return any(re.search(rx, text) for rx in DENIAL_FAMILY)


# Target absence and service unavailability are separate assertion families.
ABSENCE_COMPOUND: tuple[str, ...] = (
    "没有找到", "没有搜到", "没有查到", "没有看到", "没有发现", "没有命中", "没有匹配",
    "没找到", "没搜到", "没查到", "没看到", "没发现", "没命中", "没匹配", "没能找到",
    "未找到", "未搜到", "未查到", "未看到", "未发现", "未收录",
    "找不到", "搜不到", "查不到", "看不到", "找不着", "搜不着", "摸不到",
    "匹配不到", "对不上", "不存在", "查无", "并无",
)
# An absence assertion must identify a missing target, not just an unperformed action.
ABSENCE_LEADS: tuple[str, ...] = (
    "找到", "搜到", "查到", "看到", "发现", "命中", "匹配", "对应", "存在", "收录",
    "叫", "名为", "标题", "结果", "此条", "这一条", "这样的", "任何",
    "写着", "印着", "标着",
)
_BARE_NEG = r"(?:没有|没|未|无)"
_ABSENCE_NEG = (r"(?:" + "|".join(ABSENCE_COMPOUND)
                + rf"|{_BARE_NEG}[^。\n]{{0,4}}(?:{'|'.join(ABSENCE_LEADS)}))")
# Bound target/negation matching without crossing a sentence or newline.
ABSENCE_WINDOW = 60


def absence_pattern(target: str) -> str:
    """「目标不存在」的正则：否定形态与**字面目标**同处一小句（`。`/换行之内）。

    两个方向都收（「没找到 X」与「X 没找到」是同一件事）。目标原文来自用例声明，
    一律 `re.escape` ——它是**字面**，不是模式。
    """
    lit = re.escape(str(target))
    neg = _ABSENCE_NEG
    return (rf"(?:{neg}[^。\n]{{0,{ABSENCE_WINDOW}}}{lit}"
            rf"|{lit}[^。\n]{{0,{ABSENCE_WINDOW}}}{neg})")


def _absence_hit(text: str, targets) -> bool:
    """回复里有没有"这个目标不存在"的表述（见 `ABSENCE_COMPOUND` / `_ABSENCE_NEG`）。"""
    return any(re.search(absence_pattern(t), text) for t in targets)


# Reject asking an already authenticated caller to perform unnecessary login work.
_LOGIN_NOUN_TAIL = r"(?!用户|者)"
LOGIN_DEMAND_RE = re.compile(
    r"(?:先|去|要|需|得|请(?!求))[^。，！？\n]{0,4}(?:登录|登陆)" + _LOGIN_NOUN_TAIL
    + r"|(?:登录|登陆)" + _LOGIN_NOUN_TAIL + r"[^。，！？\n]{0,6}(?:才行|才能|才可)"
    r"|/login")


def login_demand_hit(text: str) -> "re.Match | None":
    """回复里给主人派登录的那一处（见 `LOGIN_DEMAND_RE` 的头注）；没有则 `None`。

    逐处判：被否掉/被引述的那一处跳过，同一段文本里还有站得住的照旧判。"""
    pos = 0
    while True:
        m = LOGIN_DEMAND_RE.search(text, pos)
        if not m:
            return None
        if not _mentioned_not_claimed(text, m.start(), m.end()):
            return m
        pos = m.end()


# Explicit assertion keys reject misspellings instead of silently skipping checks.
GOLD_ASSERT_KEYS = frozenset({
    "max_model_calls", "max_tool_calls", "max_article_body_chars",
    "require_observations", "require_observations_any", "require_todo_schedule",
    "require_automation_schedule", "require_confirm_intent",
    "require_confirm_specs", "require_confirm_preview", "forbid_runtime_stop",
    "forbid_business_write", "require_answer_value", "require_prepared_or_noop",
    "require_memory_compaction", "forbid_memory_compaction", "require_answer_list",
    "require_revoked_operation", "require_current_conversation_history",
    # 回复文本
    "nonempty", "text_contains", "text_any_regex", "text_not_contains",
    "text_not_match_regex", "not_contains_exempt_quote",
    "not_match_exempt_conditional",
    # Apply an explicitly enabled local-refutation exemption to the matched occurrence.
    "not_match_exempt_refuted",
    # Apply an explicitly enabled quoted-mention exemption to the matched occurrence.
    "not_match_exempt_mention",
    # Document facts are not measurements or execution evidence for the current request.
    "not_match_exempt_doc_fact",
    # A conditional consequence does not assert that the operation already happened.
    "not_match_exempt_consequence",
    # Use the shared positive-refusal family for refusal expectations.
    "require_denial",
    # 不要求已验签用户重新登录。
    "forbid_login_demand",
    # Use the shared target-absence family without equating it to service failure.
    "require_absence",
    # 命令帧（EFFECT:/DARKMODE:/NAVIGATE:…）
    "require_cmd_prefixes", "require_cmd_contains", "require_cmd_all",
    "forbid_cmd_prefixes", "forbid_cmd_contains", "either_cmd_or_text",
    # Requested tool calls.
    "require_tool_calls", "require_tool_calls_any", "no_tool_calls", "forbid_tool_calls",
    # Structured execution receipts.
    "require_exec_tools", "require_exec_args", "require_arg_from_result",
    # Receipt text must agree with the claimed effect, not merely the requested arguments.
    "require_exec_result",
    # Require at least one candidate execution and reject duplicate candidate receipts.
    "require_exec_once_any",
    # Zero-write assertions permit reads while prohibiting every declared mutation tool.
    "require_zero_exec", "forbid_exec_tools",
    # 控制帧与终局
    "require_frame_prefix", "forbid_frame_prefix", "forbid_fallback",
    # Inspect the signed proposal payload as well as whether a card appeared.
    "require_confirm_payload",
    # Archived ledger assertions inspect recorded target and provenance events.
    "require_ledger_frame", "require_ledger_rows",
    "require_card_targets_from_ledger",
    # Archived task assertions inspect the final recorded task state.
    "require_task_state", "forbid_task_state",
    # Goal-level assertions distinguish individual goals from the aggregate task state.
    "require_task_goal_per_intent",
    # 语料化（术语由申报文档运行期派生，见 eval/corpus_terms.py）
    "require_doc_terms",
})
# 由 `build_request` 消费、不进 check_gold 的请求侧键（写在 gold 里是因为它描述这条
# **用例**的请求形态，不是判据）。
GOLD_REQUEST_KEYS = frozenset({"needs_summary"})
# 只写给人看的注释键（`_note`）：不判、不读，但必须拼对——拼错等于注释不存在。
GOLD_COMMENT_KEYS = frozenset({"_note"})
# Round metadata controls the driver and is not a reply assertion.
GOLD_ROUND_KEYS = frozenset({"round", "confirm_message", "confirm_pick"})


def judge_corpus() -> "list | None":
    """Load one shared corpus snapshot for content-based assertions.

    An unavailable snapshot leaves dependent assertions unevaluated. Other
    cases continue; corpus loading is shared with provenance checks."""
    snap = corpus_snapshot_docs()
    if not snap:
        print("[corpus] 判据语料快照为空——带 require_doc_terms 的用例本轮判「未评估」")
        return None
    return snap


def _check_commands(gold, result, *, docs=None, user_input="", pending_src=""):
    text, commands = result["text"], result["commands"]
    fails = []
    if gold.get("nonempty", True) and not text.strip():
        fails.append("回复为空")

    for pre in gold.get("require_cmd_prefixes", []):
        hits = [c for c in commands if _cmd_matches(pre, c)]
        if not hits:
            fails.append(f"缺少 {pre} 命令帧")
        elif gold.get("require_cmd_contains") and not any(
            gold["require_cmd_contains"] in c for c in hits
        ):
            fails.append(f"{pre} 命令内容不符（期望含 {gold['require_cmd_contains']}）")

    # Every requested action must appear; normalize equivalent navigation URLs.
    for pat in gold.get("require_cmd_all", []):
        if not any(command_matches(pat, c) for c in commands):
            fails.append(f"命令帧缺 {pat!r}（本轮命令：{commands}）")

    for pre in gold.get("forbid_cmd_prefixes", []):
        if any(_cmd_matches(pre, c) for c in commands):
            fails.append(f"不应产生 {pre} 命令帧")
    for kw in gold.get("forbid_cmd_contains", []):
        if any(kw in c for c in commands):
            fails.append(f"命令帧不应包含 {kw!r}")

    # 二选一：产生过命令帧（动作执行）或正文含关键词（诚实拒绝并给入口，如未登录告知）
    if gold.get("either_cmd_or_text"):
        kw = gold["either_cmd_or_text"]
        has_cmd = any(c.startswith(CMD_PREFIXES) for c in commands)
        if not (has_cmd or kw in text):
            fails.append(f"既无命令帧，文本也未含 {kw!r}（期望执行动作或诚实拒绝并给出入口）")

    return fails


def _check_text(gold, result, *, docs=None, user_input="", pending_src=""):
    text = result["text"]
    fails = []
    kws = gold.get("text_contains", [])
    regexes = gold.get("text_any_regex", [])
    # Positive refusal evidence is another member of the same any-of assertion.
    denial = bool(gold.get("require_denial"))
    # Positive target-absence evidence is another member of the same any-of assertion.
    _ab = gold.get("require_absence")
    _abs_targets = [_ab] if isinstance(_ab, str) else list(_ab or [])
    if kws or regexes or denial or _abs_targets:
        hit = (any(kw in text for kw in kws) or any(re.search(rx, text) for rx in regexes)
               or (denial and _denial_hit(text))
               or (_abs_targets and _absence_hit(text, _abs_targets)))
        if not hit:
            fails.append(f"文本缺少任一关键词 {kws!r} 且未命中正则族 {regexes!r}"
                         + ("（含共享的诚实拒绝族 DENIAL_FAMILY）" if denial else "")
                         + ("（含共享的『目标不存在』族）"
                            if _abs_targets else ""))
    # Quotation exemptions apply only when explicitly enabled by the case.
    exempt = gold.get("not_contains_exempt_quote", False)
    for kw in gold.get("text_not_contains", []):
        if _forbidden_hit(text, kw, exempt):
            fails.append(f"文本不应包含 {kw!r}")
    # Forbidden regexes complement the positive reply assertions.
    ex = Exempts.from_gold(gold)
    for rx in gold.get("text_not_match_regex", []):
        m = _forbidden_regex_hit(text, rx, ex)
        if m:
            fails.append(f"文本不应命中正则 {rx!r}（命中片段 {m.group(0)!r}）")
    # Reject needless login instructions to an authenticated caller.
    if gold.get("forbid_login_demand"):
        _ld = login_demand_hit(text)
        if _ld:
            fails.append(f"文本不应给主人派登录的活（命中片段 {_ld.group(0)!r}）")

    return fails


def _check_ledger(gold, result, *, docs=None, user_input="", pending_src=""):
    fails = []
    _lf_events = result.get("ledger_frames") or []
    _ledger_ids = [str(i) for e in _lf_events for i in (e.get("ids") or [])]
    # Missing trace evidence makes trace-dependent assertions unevaluated.
    _lf_off = bool(not golden_trace.enabled()
                   and [k for k in golden_trace.TRACE_DERIVED_GOLD_KEYS if k in gold])
    if _lf_off:
        fails.append("[未评估] 本轮关着 trace（--no-trace / GOLDEN_NO_TRACE=1）⇒ "
                     "planner.ledger_frame 事件不存在 ⇒ 台账那几条判据全都判不了"
                     "（未评估 ≠ 通过；要判就开着 trace 跑）")
    if gold.get("require_ledger_frame") and not _lf_off:
        if not _lf_events:
            fails.append("本轮没有 planner.ledger_frame 事件 —— 待办台账**没摆上桌**"
                         "（模型手里没有可决策的目标）")
    if gold.get("require_ledger_rows") and not _lf_off:
        if not _lf_events:
            fails.append("本轮没有 planner.ledger_frame 事件 —— 待办台账**没摆上桌**，"
                         "连「有没有等着办的行」都无从判起")
        elif not _ledger_ids:
            fails.append(f"台账摆了但一条待办都没有（事件：{_lf_events}）"
                         "——本键要求帧里至少印出一个编号（要有待办行才判得动）")
    if gold.get("require_card_targets_from_ledger") and not _lf_off:
        from agent.identifiers import normalize_target_id  # 编号解析的唯一实现
        from eval.native_receipt_contract import FIELD_FAMILY, TAG_FAMILY, TARGET_FIELDS
        _byfam: dict = {}
        for _e in _lf_events:
            for _raw in (_e.get("ids") or []):
                _s = str(_raw)
                _tag, _separator, _value = _s.partition(":")
                _fam = TAG_FAMILY.get(_tag)
                _tid = normalize_target_id(_value)
                if _fam and _tid is not None:
                    _byfam.setdefault(_fam, set()).add(int(_tid))
        _seen, _bad = 0, []
        for _p in (result.get("confirm_payloads") or []):
            for _sp in (_p or {}).get("specs") or []:
                _tool = str((_sp or {}).get("tool") or "")
                _field = TARGET_FIELDS.get(_tool)
                if not _field:
                    continue
                _seen += 1
                _val = ((_sp or {}).get("args") or {}).get(_field)
                _tid = normalize_target_id(_val)
                _fam = FIELD_FAMILY.get(_field, "")
                if _tid is None or int(_tid) not in _byfam.get(_fam, set()):
                    _bad.append(f"{_tool}.{_field}={_val!r}")
        if not _seen:
            # A target-ID assertion requires at least one identified proposal target.
            fails.append("卡片里没有一件按台账编号的目标（本键无从判起 ⇒ 判红）")
        if _bad:
            fails.append(f"卡片上的台账编号不来自本轮帧：{_bad}"
                         f"（本轮帧里印的编号：{_ledger_ids}）")

    return fails


def _check_task_results(gold, result, *, docs=None, user_input="", pending_src=""):
    fails = []
    _tf_states = [str(f.get("state") or "") for f in (result.get("task_frames") or [])]
    _req_ts = gold.get("require_task_state")
    if _req_ts:
        if not _tf_states:
            fails.append(f"本轮没有发出 __TASK__ 帧（期望末帧 state ∈ {_req_ts}）")
        elif _tf_states[-1] not in _req_ts:
            fails.append(f"任务末帧 state={_tf_states[-1]!r}，期望 ∈ {_req_ts}"
                         f"（本轮全部帧：{_tf_states}）")
    for s in gold.get("forbid_task_state", []):
        if s in _tf_states:
            fails.append(f"任务状态不应是 {s!r}（本轮帧：{_tf_states}）")

    # Check per-goal task outcomes independently of aggregate task status.
    _per_intent = gold.get("require_task_goal_per_intent")
    if _per_intent:
        _tfs = [f for f in (result.get("task_frames") or [])
                if isinstance(f, dict) and str(f.get("state") or "") != "cancelled"]
        _goals = [outcome_assertions.squash_ws(str(f.get("goal") or "")) for f in _tfs]
        _carded = ""
        for _p in (result.get("confirm_payloads") or []):
            if isinstance(_p, dict) and _p.get("workflow"):
                _carded = str(_p["workflow"])
                break
        for _it in _per_intent:
            if str(_it.get("workflow") or "") == _carded:
                continue                     # 这一件上了卡 ⇒ 不在本条射程内
            _want = outcome_assertions.squash_ws(str(_it.get("goal") or ""))
            if _want and any(_want in _g for _g in _goals):
                continue
            fails.append(
                f"没上卡的那件事没有留下任务登记：期望有一帧 __TASK__ 的 goal 含 "
                f"{_it.get('goal')!r}（这件事的卡片技能是 {_it.get('workflow')!r}，"
                f"本轮卡片认领的是 {_carded or '（没有卡片载荷）'}）"
                f"；本轮帧里的 goal（撤下帧已排除）：{[f.get('goal') for f in _tfs]}")

    _fb = fallback_resets(result)
    if gold.get("forbid_fallback") and _fb:
        fails.append(f"本轮出现终止降级回复（{_fb}）——用户收到的是兜底道歉，"
                     f"正断言命中的是道歉文本，不算通过"
                     + (f"；本轮另有 {result['resets'] - len(_fb)} 次打回重规划"
                        f"（scope=all，最终文本是重规划后的，不算兜底）"
                        if result["resets"] > len(_fb) else ""))

    return fails


_GOLD_ASSERTIONS = (
    _check_commands,
    _check_text,
    outcome_assertions.check_tools,
    outcome_assertions.check_confirmation,
    _check_ledger,
    _check_task_results,
    outcome_assertions.check_execution_results,
    outcome_assertions.check_documents,
)


def check_gold(gold: dict, result: dict, *, docs=None, user_input: str = "",
               pending_src: str = "") -> list[str]:
    """Validate declared task outcomes using independent business assertions."""
    fails = check_native(gold, result)
    for assertion in _GOLD_ASSERTIONS:
        fails.extend(assertion(gold, result, docs=docs, user_input=user_input,
                               pending_src=pending_src))
    return fails


# Use the shared Wilson interval implementation.
