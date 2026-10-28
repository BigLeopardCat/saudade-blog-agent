"""Frozen no-popup question predicate for archived evaluation data.

This code does not authorize tools or classify live user intent. It preserves the
old premise checker so historical corpora remain comparable during retirement.
"""

import re

_CONSOLE_QUESTION_RE = re.compile(
    r"[？?]|吗|呢|怎么|为什么|为啥|是否|能否|能不能|可不可以|会不会|要不要|"
    r"有什么影响|有何影响|有没有|是不是|该不该|应不应该|好不好|行不行|对不对|"
    r"对吧|是吧|对吗|不是吗|怎么办|咋办|什么后果|风险")

_CONSOLE_HYPOTHESIS_RE = re.compile(
    r"^\s*(?:如果|假如|假设|要是|若是|万一|倘若|我想问|我想知道|想问|请问|问一下)")

_SYS_TAG_RE = re.compile(r"^(?:\s*\[[^\[\]\n]{0,32}\]\s*[:：]?\s*)+")


def _strip_system_tags(text: str) -> str:
    return _SYS_TAG_RE.sub("", text or "", count=1)


_VOCATIVE_RE = re.compile(
    r"^\s*(?:(?:小|大|傻|臭|笨|乖)?(?:猫咪|猫猫)|泠月喵|泠月|喵喵|喵|主人)"
    r"(?:\s*[，,、:：!！~～。.…]+\s*|\s+)?")


def _strip_address_shell(text: str) -> str:
    return _VOCATIVE_RE.sub("", text or "", count=1)


def _strip_user_shell(text: str | None) -> str:
    return _strip_address_shell(_strip_system_tags((text or "").strip()))


def is_question_like(msg: str) -> bool:
    text = _strip_user_shell(msg)
    if not text:
        return True
    return bool(
        _CONSOLE_QUESTION_RE.search(text)
        or _CONSOLE_HYPOTHESIS_RE.search(text)
        or _CONSOLE_INQUIRY_RE.search(text)
    )


_INQUIRY_NOUNS = r"步骤|流程|条件|要求|影响|后果|风险|注意|区别|好处|坏处"

_CONSOLE_INQUIRY_RE = re.compile(
    r"是什么|是啥|有什么|有多少|多少|多久|几天|可以吗|行吗"
    r"|的(?:" + _INQUIRY_NOUNS + r")"
    r"|(?:什么|哪些)(?:" + _INQUIRY_NOUNS + r")")
