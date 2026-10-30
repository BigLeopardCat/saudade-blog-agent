"""Resolve the embedding space shared by graph building and querying.

Only standard-library dependencies are allowed because graph builds run in an
isolated environment. Explicit EMBEDDING_* configuration takes precedence; the
fallback uses QWEN_* independently of the chat provider. FALLBACK_MODEL selects
new vectors; FLAT_CACHE_MODEL identifies unsigned legacy cache vectors. Never
merge vectors with different model or endpoint identities.

Offline BUILD_TIMEOUT is separate from the shorter query timeout, which must
fit within the host request deadline."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

# Current fallback model for new graph vectors; independent of the chat provider.
FALLBACK_MODEL = "qwen3.7-text-embedding"
# Unsigned flat caches identify a fixed historical model. Never change this identity
# when changing the model used to generate new vectors.
FLAT_CACHE_MODEL = "text-embedding-v4"
LEGACY_DIM = 1024
LEGACY_BATCH = 10              # 百炼 text-embedding 单请求 input 上限 10 条
# `EMBEDDING_BASE_URL` 留空 = SDK 默认端点（与 openai SDK 的语义一致）。
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
# 建图侧单请求超时：离线任务，宁可等（重试退避 1s/3s/9s）。**不是** EMBEDDING_TIMEOUT。
BUILD_TIMEOUT = 30.0

# 占位串：与 `Settings.embedding_configured` 逐字一致，**别在这里自作主张多认几个**
# （多认一个 = 两条规则在那一格上分叉，而这一整块的存在意义就是它们不分叉）。
_PLACEHOLDER_KEYS = frozenset({"your-api-key-here"})

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Space:
    """一次 embedding 调用所处的那片空间。字段就是它的全部身份。

    ``api_key`` 标了 ``repr=False``：这个对象可能被顺手打进日志/异常信息里，
    而"密钥不进 stdout/日志"是这个项目的硬纪律——想打日志请打 ``describe()``。
    """

    source: str                 # "embedding" | "qwen" —— 从哪一格配置解出来的
    model: str
    base_url: str               # 已归一化（去尾斜杠），`signature()` 也用它
    api_key: str = field(repr=False)
    dim: int = 0                # 0 = **不向 API 传 `dimensions`**，以返回长度为准
    batch: int = LEGACY_BATCH

    def describe(self) -> str:
        """给人看的来源说明，**不含 key**。"""
        return (f"来源 {self.source}（model={self.model} / 维度 "
                f"{self.dim or '未指定'} / 批 {self.batch}）")


def _clean(raw) -> str:
    """取一个环境变量值：None → ""，剥白/引号。"""
    if raw is None:
        return ""
    text = str(raw).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _norm(url: str) -> str:
    """端点归一化：只去尾斜杠。

    刻意**不**做大小写/默认端口/等价的规范化——这一格只在"旧缓存认不认"这一个判据上
    用到，而那个判据的失败方向必须是**重嵌一次**（贵一点、但对），不是"认了两个其实
    不同的端点"（便宜、但错得没有声音）。
    """
    return (url or "").strip().rstrip("/")


def _int(raw, default: int, minimum: int = 0) -> int:
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


def _key_usable(key: str) -> bool:
    return bool(key) and key not in _PLACEHOLDER_KEYS


def resolve(env: Mapping[str, str]) -> Space:
    """从一份（`.env` 同形的）映射解出这次该用哪个空间。

    ``env`` 传什么都行：``os.environ``、建图脚本自己解析的 `.env`、或测试里随手写的 dict。
    """
    model = _clean(env.get("EMBEDDING_MODEL"))
    key = _clean(env.get("EMBEDDING_API_KEY"))
    if model and _key_usable(key):
        return Space(
            source="embedding", model=model,
            base_url=_norm(_clean(env.get("EMBEDDING_BASE_URL"))) or OPENAI_DEFAULT_BASE,
            api_key=key,
            # 0 是合法值（=不传 dimensions），所以这里 minimum=0；垃圾值也退回 0
            dim=_int(env.get("EMBEDDING_DIM"), 0),
            batch=_int(env.get("EMBEDDING_BATCH_SIZE"), LEGACY_BATCH, minimum=1),
        )
    return Space(
        source="qwen", model=FALLBACK_MODEL,
        base_url=_norm(_clean(env.get("QWEN_BASE_URL"))),
        api_key=_clean(env.get("QWEN_API_KEY")),
        dim=LEGACY_DIM, batch=LEGACY_BATCH,
    )


def env_of(settings) -> dict:
    """把 pydantic ``Settings`` 拍成 ``resolve()`` 要的那种 dict。

    只做取值、不 import 任何东西——这样查询侧与建图脚本走的是同一个 ``resolve()``。
    """
    return {
        "EMBEDDING_MODEL": getattr(settings, "embedding_model", ""),
        "EMBEDDING_API_KEY": getattr(settings, "embedding_api_key", ""),
        "EMBEDDING_BASE_URL": getattr(settings, "embedding_base_url", ""),
        "EMBEDDING_DIM": getattr(settings, "embedding_dim", 0),
        "EMBEDDING_BATCH_SIZE": getattr(settings, "embedding_batch_size", LEGACY_BATCH),
        "QWEN_API_KEY": getattr(settings, "qwen_api_key", ""),
        "QWEN_BASE_URL": getattr(settings, "qwen_base_url", ""),
    }


def space_of(settings) -> Space:
    return resolve(env_of(settings))


def missing_config(space: Space) -> str | None:
    """Return a shared diagnostic for unusable embedding configuration.

    Explicit embedding configuration supplies a key and endpoint. Fallback
    configuration may lack either; identify the missing input without exposing it."""
    if space.api_key and space.base_url:
        return None
    if space.source == "qwen":
        empty = [name for name, val in (("QWEN_API_KEY", space.api_key),
                                        ("QWEN_BASE_URL", space.base_url)) if not val]
        return ("、".join(empty) + " 是空的，而 EMBEDDING_API_KEY + EMBEDDING_MODEL "
                "那一组也不齐（配了就优先用它）")
    empty = [name for name, val in (("EMBEDDING_API_KEY", space.api_key),
                                    ("EMBEDDING_BASE_URL", space.base_url)) if not val]
    return "、".join(empty) + " 是空的"


def signature(space: Space) -> dict:
    """写进缓存文件头的那几格。**不含 key**（缓存是盘上的普通文件，不装凭据）。

    ``dim`` 记的是**请求**维度（``0`` = 没传 dimensions），与 `rag/vector_index.py`
    的 `Space` 同口径：两片空间是不是同一片，看请求怎么发，而不是看服务返回了多少维。
    """
    return {"model": space.model, "base_url": _norm(space.base_url), "dim": space.dim}


def legacy_cache_ok(space: Space, env: Mapping[str, str]) -> bool:
    """Accept an unsigned legacy cache only when model and endpoint both match.

    The immutable flat-cache model identity differs from the configurable current
    fallback. A mismatch requires re-embedding, never combining incompatible vectors."""
    if space.model != FLAT_CACHE_MODEL or space.dim not in (0, LEGACY_DIM):
        return False
    base = _norm(space.base_url)
    return bool(base) and base == _norm(_clean(env.get("QWEN_BASE_URL")))


def _is_vec(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value))


def read_cache(path, space: Space, env: Mapping[str, str], log=print) -> dict:
    """读向量缓存，键 = ``md5(词)``。

    **认不出来就整份当空，绝不半读**：半读会把两代向量混进同一张图，而图不会报错，
    只会默默变得不对。文件坏掉/不是 dict/空间不符 ⇒ 全部重嵌（贵，但对）。
    单条向量坏掉只丢那一条（它是值级损坏，不是空间级，重嵌那一个词就够）。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:                                    # noqa: BLE001
        log(f"  ⚠ 缓存文件读不动（{type(e).__name__}），当作空缓存重嵌")
        return {}
    if not isinstance(raw, dict):
        log("  ⚠ 缓存文件不是一个对象，当作空缓存重嵌")
        return {}

    if isinstance(raw.get("vecs"), dict):                     # 新格式：带空间签名
        if raw.get("space") != signature(space):
            log(f"  ⚠ 缓存是另一个空间建的（{raw.get('space')} ≠ {signature(space)}），"
                f"整份作废重嵌")
            return {}
        vecs = raw["vecs"]
    elif legacy_cache_ok(space, env):                         # 旧格式：平铺的 md5 → 向量
        vecs = raw
    else:
        log("  ⚠ 旧格式缓存（无空间签名），无法确认是同一片空间，整份作废重嵌")
        return {}

    good = {k: v for k, v in vecs.items() if _MD5_RE.match(k) and _is_vec(v)}
    dropped = len(vecs) - len(good)
    if dropped:
        log(f"  ⚠ 缓存里 {dropped} 条不是合法向量，已丢弃（会重嵌那几个词）")
    return good


def cache_payload(space: Space, vecs: Mapping[str, Iterable[float]]) -> dict:
    """写回缓存用的完整载荷（调用方每批整份重写，崩溃语义与从前一致）。"""
    return {"space": signature(space), "vecs": vecs}
