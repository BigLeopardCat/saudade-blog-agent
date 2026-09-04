#!/usr/bin/env python3
"""20260904 sticker smoke：情绪输入 → 观察 :名字: 引用（频率/位置/堆砌/编造外名）+ 中性输入 → 观察不用。"""
import json
import re
import sys
import time
import urllib.request

KNOWN = ["头疼", "委屈", "害羞", "比耶", "犯错", "生气", "贴贴", "震惊"]
MARKER = re.compile(r":([^:\s]{1,12}):")

CASES = [
    # (标签, 消息, 预期情绪类)
    ("被夸_害羞", "泠月喵你太可爱了吧！回答得又快又准，我超喜欢你的！", "害羞"),
    ("用户喜事_比耶", "我搞了三天的一个bug终于调通了！原来是少了个分号，太开心了哈哈哈", "比耶"),
    ("难题_安慰", "主人我快被这个bug折磨疯了，日志看不出任何问题但它就是崩溃，好想哭", "贴贴"),
    ("中性_限制检查", "帮我查一下博客里有哪些文章吧", None),  # 中性检索 → 应不用表情
]

def ask(msg):
    body = json.dumps({"message": msg, "current_url": "", "page_title": "",
                       "history": [], "summary": "", "current_effects": "",
                       "current_darkmode": "", "user_id": 0}).encode()
    req = urllib.request.Request("http://127.0.0.1:8010/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return data.get("reply", ""), round(time.time() - t0)

fails = 0
for tag, msg, expect in CASES:
    try:
        reply, secs = ask(msg)
    except Exception as e:
        print(f"[{tag}] ERROR {e}")
        fails += 1
        continue
    marks = MARKER.findall(reply)
    invented = [m for m in marks if m not in KNOWN]
    tail = marks[-1] if marks else ""
    ok_pos = not marks or tail in reply[-80:]  # 情绪句句尾/末段内即可（多段回复不强制整段收尾）
    verdict = []
    if expect is not None and not marks:
        verdict.append("缺引用")
    if expect is None and marks:
        verdict.append("中性仍引用")
    if len(marks) > 1:
        verdict.append(f"堆砌({len(marks)}个)")
    if invented:
        verdict.append(f"编造外名{invented}")
    if not ok_pos:
        verdict.append("位置不自然")
    status = "PASS" if not verdict else "FAIL " + "/".join(verdict)
    if status != "PASS":
        fails += 1
    print(f"[{tag}] {status} ({secs}s) 期望{expect or '无'}")
    print(f"  marks={marks}")
    print(f"  reply={reply[:300]}")
print("FAILS:", fails)
sys.exit(1 if fails else 0)
