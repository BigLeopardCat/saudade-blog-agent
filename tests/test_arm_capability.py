# -*- coding: utf-8 -*-
"""`eval/arm_capability.py`（能力台账）单测：离线、秒级、零网络、零生产路径。

**为什么这份仪器要单独测**：它的失效方式是**沉默且温柔**的——把"未武装"报成"已武装"，
于是下游的 `pass_rate` / `--ab` 照常出数、照常好看，而那个"打平"其实来自"这台机器什么都
没产出、所以什么都没违规"。所以这里钉的不是"函数能跑"，而是**判据本身不许松**：

  ① 逐族的"见证者"：报告里**真看得见**才算见过（`frames` 里没有 `__TASK__` 就是没有）；
  ② 最要命的一条：`scope=all` 的 `__RESET__` **不是**兜底（用户收到的是重查后的真回答），
     拿它当 `forbid_fallback` 的见证 = 把"打回重规划"读成"用户收到了道歉"；
  ③ 正控**不红就不算数**（`falsifiable:false` 时这一族必须仍然未武装）——正控的全部价值
     就在这一句上；
  ④ 退出码：不可比 ⇒ 3（未评估），不是 0。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import arm_capability as ac  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _corpus_line(cid: str, gold: dict, *, rounds: list | None = None) -> dict:
    if rounds is None:
        return {"id": cid, "user_input": "x", "gold": gold, "context": {}}
    return {"id": cid, "rounds": [{"gold": g, "user_input": "x"} for g in rounds]}


# ── ① 逐族见证者 ─────────────────────────────────────────────────────────────
print("\n① 见证者：只有报告里真看得见的东西才算见过")
check("commands 空 ⇒ 没见过", not ac._w_cmd({}, []))
check("commands 非空 ⇒ 见过", ac._w_cmd({"commands": ["__CMD__:x"]}, []))
check("逐轮里的 commands 也认（报告两层都看）",
      ac._w_cmd({"rounds": [{"commands": []}, {"commands": ["c"]}]}, []))
check("tool_calls 只在逐轮里也认", ac._w_tool({"rounds": [{"tool_calls": ["list_notes"]}]}, []))
check("exec_tools 只在逐轮里（用例级没有这一格）",
      ac._w_exec({"rounds": [{"exec_tools": ["delete_category"]}]}, []))
check("exec_tools 空 ⇒ 没见过", not ac._w_exec({"rounds": [{"exec_tools": []}]}, []))

_conf = {"rounds": [{"frames": ['__CONFIRM__:{"q": "…"}']}]}
check("帧前缀见证：禁的前缀出现过 ⇒ 见过", ac._w_frame_prefix(_conf, ["__CONFIRM__:", "__PENDING__:"]))
check("帧前缀见证：只见过别的帧 ⇒ 没见过",
      not ac._w_frame_prefix({"rounds": [{"frames": ["__PENDING__:{}"]}]}, ["__CONFIRM__:"]))
check("帧前缀见证：语料没写禁用值 ⇒ **不许**默认见过（否则空列表恒真）",
      not ac._w_frame_prefix(_conf, []))
check("__TASK__ 帧（进 frames 那种）⇒ 见过",
      ac._w_task({"rounds": [{"frames": ["__TASK__:{}"]}]}, []))
check("逐轮 task_frames 非空 ⇒ 见过（20261004 起报告新留的那一格）",
      ac._w_task({"rounds": [{"task_frames": [{"id": "t1", "state": "cancelled"}]}]}, []))
check("两个地方都没有 ⇒ 没见过", not ac._w_task({"rounds": [{"frames": [], "task_frames": []}]}, []))

print("\n①b `scope=all` 不是兜底 —— 这一条读错就是把「重查后如实回答」当成「用户收到了道歉」")
check("text-scope ⇒ 见过兜底", ac._w_fallback({"reset_scopes": ["text"]}, []))
check("**只有 all-scope ⇒ 没见过**（打回重规划不是兜底）",
      not ac._w_fallback({"reset_scopes": ["all"]}, []))
check("逐轮的 reset_scopes 也认", ac._w_fallback({"rounds": [{"reset_scopes": ["text"]}]}, []))
check("没有 reset_scopes 这一格（老报告）⇒ 没见过，不猜",
      not ac._w_fallback({"resets": 3, "resets_reasons": ["…"]}, []))


# ── ② 语料装载：多轮并集 + 取值也留 ──────────────────────────────────────────
print("\n② 语料装载")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "c.jsonl"
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        _corpus_line("multi", {}, rounds=[{"forbid_cmd_prefixes": ["AUTO_NAVIGATE"]},
                                          {"forbid_frame_prefix": ["__CONFIRM__:"]}]),
        _corpus_line("plain", {"require_cmd_prefixes": ["AUTO_NAVIGATE"]}),
    ]) + "\n", encoding="utf-8")
    corp = ac.load_corpus(p)
check("多轮用例的 gold 键按轮并集", {c["id"]: c["keys"] for c in corp}["multi"]
      == {"forbid_cmd_prefixes", "forbid_frame_prefix"})
check("取值也留下来（见证者照着语料自己写的值认前缀）",
      corp[0]["values"]["forbid_frame_prefix"] == ["__CONFIRM__:"])

# ── ③ 记账：武装与非武装 ─────────────────────────────────────────────────────
print("\n③ 记账：谁被武装、谁不可读")
_sib_pass = {"cases": [{"id": "plain", "ok": True, "commands": ["__CMD__:AUTO_NAVIGATE:x"]}]}
out = ac.analyze("react", corpus=corp, reports=[_sib_pass])
check("正向兄弟通过 ⇒ 该族 armed", out["families"]["forbid_cmd_prefixes"]["armed"])
check("正向兄弟通过是**记名**的（读的人能去核）",
      out["families"]["forbid_cmd_prefixes"]["armed_by_sibling"] == ["plain"])
check("没有正向兄弟、也没有实证 ⇒ forbid_fallback 未武装",
      not out["families"]["forbid_fallback"]["armed"])
check("未武装族进 unarmed_families", "forbid_fallback" in out["unarmed_families"])
check("有一条不可证伪的键 ⇒ 该用例进 unfalsifiable_ids",
      out["unfalsifiable_ids"] == ["multi"] and out["comparable_ids"] == ["plain"])
check("整体不可比", not out["comparable"])
check("**退出码 3 = 未评估**（不是 0）", ac.exit_code(out) == 3)

print("\n③b 合成正控：红了才算数")
_red = {"falsifiable": True, "detail": "强制兜底一次后 forbid_fallback 变红 ✅"}
out_r = ac.analyze("react", corpus=corp, reports=[_sib_pass], probe=_red)
check("正控为红 ⇒ forbid_fallback 武装", out_r["families"]["forbid_fallback"]["armed"])
# 这一份小语料只断言两族：cmd（兄弟武装）+ fallback（正控武装）⇒ 两族都武装 ⇒ 可比。
_two = [{"id": "plain", "keys": {"forbid_cmd_prefixes"}, "values": {}},
        {"id": "fb", "keys": {"forbid_fallback"}, "values": {}}]
out_ok = ac.analyze("react", corpus=_two, reports=[_sib_pass], probe=_red)
check("含断言的族全部武装 ⇒ comparable，且退出码 0",
      out_ok["comparable"] and ac.exit_code(out_ok) == 0)
check("语料里没人断言的一族即使未武装，也不影响可比（否则 comparable 会被永久钉死）",
      out_ok["unarmed_families"] and not out_ok["unarmed_asserted_families"])
check("正控未红（falsifiable:false）⇒ **仍然未武装**（正控的价值全在这一句）",
      not ac.analyze("react", corpus=corp, reports=[_sib_pass],
                     probe={"falsifiable": False, "detail": "没变红 ❌"}
                     )["families"]["forbid_fallback"]["armed"])

print("\n③c 见证不限于「断言了这一族的用例」（指涉物在别处的运行里出现同样算数）")
_wit_elsewhere = {"cases": [{"id": "other", "ok": False,
                             "rounds": [{"tool_calls": ["list_notes"]}]}]}
out_w = ac.analyze("react", corpus=[{"id": "w", "keys": {"forbid_tool_calls"}, "values": {}}],
                   reports=[_wit_elsewhere])
check("别处的运行里真的调过工具 ⇒ forbid_tool_calls 武装（否则会把它误判成不可读）",
      out_w["families"]["forbid_tool_calls"]["armed"])

print("\n③d 空归档不是「全绿」而是「全不可比」")
out_e = ac.analyze("react", corpus=corp, reports=[])
check("一份归档都没有 ⇒ 未武装族=全部纯否定族", len(out_e["unarmed_families"]) == len(ac.FAMILIES))
check("空归档 ⇒ comparable False + 退出码 3", not out_e["comparable"] and ac.exit_code(out_e) == 3)
check("空归档给出 caveat（读的人知道为什么）", bool(out_e["reports"]["caveat"]))

print("\n④ 通过例的键名跨版本兼容（`ok` / `final_ok`）")
check("老报告的 final_ok 也认", ac._passed({"final_ok": True}))
check("ok 优先于 final_ok", not ac._passed({"ok": False, "final_ok": True}))

print("\n" + "=" * 56)
if FAILS:
    print(f"❌ {len(FAILS)} 项未过：")
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("✅ 全部通过")
