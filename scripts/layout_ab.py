#!/usr/bin/env python3
"""三维布局的离线 A/B 夹具（20260917）——**同语料 / 同 embedding / 同边集，只换布局**。

为什么单独留一个脚本：布局算法的选择是这个项目里最容易"凭感觉"的一处，而它直接决定
访客点一个词之后看到的是不是相关内容。20260917 用它做了决定（结论见 build_word_graph.py
的 layout_umap docstring），以后想再换布局/调参，改这里重跑，别凭观感。

口径与质量门**完全一致**（复用 build_word_graph 里的 fidelity / spearman / 边集）：
  保真度 = 视图 10-NN vs 处理空间 10-NN（越高越好；随机基线 10/(n-1)≈0.025）
  rho    = spearman(边长, 相似度)（越负越"相似=线短"；**它不是门**，理由见
           build_word_graph.py 里那段注释：只在边集上优化即可做到 −1.000 而保真度随机）

用法：
    uv run --no-project --python 3.12 --with-requirements scripts/requirements-graph.txt \
        python3 scripts/layout_ab.py --variant umap
    ... --variant baseline|umap|umap_spring|isomap_knn|isomap_edges|smacof_edges
    （umap/umap_spring 需要 umap-learn；其余纯 numpy）
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_word_graph as B  # noqa: E402

STATE: dict = {}


def _finalize(pos, clip):
    """与 build_word_graph._finalize_layout 逐字一致（居中 → 98 分位归一 → 夹取）：
    比的是布局差异，不是尺度差异。"""
    return B._finalize_layout(pos, clip)


# ────────────────────────────── 布局变体

def layout_isomap_knn(sim, clip, k):
    """在**处理空间自建 kNN 图**（k 通常 10~30，不是产物里那条平均度 3.5 的稀疏边集）
    → 测地距离 → 经典 MDS 三维。纯 numpy。"""
    n = sim.shape[0]
    xn = sim / np.maximum(np.linalg.norm(sim, axis=1, keepdims=True), 1e-12)
    sims = xn @ xn.T
    np.fill_diagonal(sims, -1.0)
    BIG = 6.0
    D = np.full((n, n), BIG, dtype=np.float64)
    np.fill_diagonal(D, 0.0)
    for i in range(n):
        for j in np.argpartition(sims[i], -k)[-k:]:
            d = 1.0 - float(sims[i, j])
            if d < D[i, j]:
                D[i, j] = D[j, i] = d
    for kk in range(n):                      # Floyd-Warshall（n=400 足够快）
        D = np.minimum(D, D[:, kk, None] + D[None, kk, :])
    J = np.eye(n) - np.ones((n, n)) / n
    Bm = -0.5 * J @ (D ** 2) @ J
    w, v = np.linalg.eigh(Bm)
    idx = np.argsort(w)[::-1][:3]
    return _finalize(v[:, idx] * np.sqrt(np.maximum(w[idx], 1e-12)), clip)


def layout_isomap_edges(sim, edges, clip):
    """同上，但测地距离跑在**产物那条稀疏边集**上（对照：说明"图太稀 ⇒ 测地距离不可靠"）。"""
    n = sim.shape[0]
    BIG = 4.0
    D = np.full((n, n), BIG, dtype=np.float64)
    np.fill_diagonal(D, 0.0)
    for a, b, s in edges:
        D[a, b] = D[b, a] = min(D[a, b], 1.0 - float(s))
    for kk in range(n):
        D = np.minimum(D, D[:, kk, None] + D[None, kk, :])
    J = np.eye(n) - np.ones((n, n)) / n
    Bm = -0.5 * J @ (D ** 2) @ J
    w, v = np.linalg.eigh(Bm)
    idx = np.argsort(w)[::-1][:3]
    return _finalize(v[:, idx] * np.sqrt(np.maximum(w[idx], 1e-12)), clip)


def layout_smacof_edges(sim, edges, clip, iters=300):
    """教科书 SMACOF（应力主化）：只在**边集**上优化"图上距离 ≈ 1−相似度"。
    ⚠️ 这是"rho 可被游戏"的实证——它把 rho 做到 −1.000 而保真度塌到随机。"""
    n = sim.shape[0]
    D = np.zeros((n, n), dtype=np.float64)
    W = np.zeros((n, n), dtype=np.float64)
    for a, b, s in edges:
        D[a, b] = D[b, a] = 1.0 - float(s)
        W[a, b] = W[b, a] = 1.0
    J = np.eye(n) - np.ones((n, n)) / n
    B0 = -0.5 * J @ (D ** 2) @ J
    w, v = np.linalg.eigh(B0)
    idx = np.argsort(w)[::-1][:3]
    pos = v[:, idx] * np.sqrt(np.maximum(w[idx], 1e-12))
    V = -W.copy()
    np.fill_diagonal(V, W.sum(axis=1))
    Vp = np.linalg.pinv(V)                   # 常数 → 只算一次
    for _ in range(iters):
        dist = np.maximum(np.linalg.norm(pos[:, None, :] - pos[None, :, :], axis=2), 1e-9)
        Bm = -W * np.where(W > 0, D / dist, 0.0)
        np.fill_diagonal(Bm, -Bm.sum(axis=1))
        pos = Vp @ Bm @ pos
    return _finalize(pos, clip)


# ────────────────────────────── 打桩：截住处理空间向量 + 替换布局

_ARGS: argparse.Namespace
_orig_project = B.project_3d
_orig_layout = B.layout_semantic


def _patched_project(*a, **kw):
    p, meta, tr = _orig_project(*a, **kw)
    STATE["sim"] = tr["nodes"]               # 处理空间向量（连边/指标/查询同一套语义）
    return p, meta, tr


def _patched_layout(pca_p, edges, iters=400, clip=1.6, **kw):
    sim, v = STATE["sim"], _ARGS.variant
    if v == "baseline":
        return _orig_layout(pca_p, edges, iters=iters, clip=clip, **kw)
    if v == "umap":
        return B.layout_umap(sim, clip, _ARGS.n_neighbors, _ARGS.min_dist, _ARGS.seed)
    if v == "umap_spring":
        init = B.layout_umap(sim, clip, _ARGS.n_neighbors, _ARGS.min_dist, _ARGS.seed)
        return _orig_layout(init, edges, iters=_ARGS.iters, clip=clip, anchor=_ARGS.anchor, **kw)
    if v == "isomap_knn":
        return layout_isomap_knn(sim, clip, _ARGS.n_neighbors)
    if v == "isomap_edges":
        return layout_isomap_edges(sim, edges, clip)
    if v == "smacof_edges":
        return layout_smacof_edges(sim, edges, clip)
    raise SystemExit(f"未知 variant={v}")


def main() -> None:
    global _ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="baseline",
                    choices=("baseline", "umap", "umap_spring", "isomap_knn",
                             "isomap_edges", "smacof_edges"))
    ap.add_argument("--n-neighbors", type=int, default=15, dest="n_neighbors")
    ap.add_argument("--min-dist", type=float, default=0.05, dest="min_dist")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--iters", type=int, default=400, help="umap_spring 的弹簧迭代数")
    ap.add_argument("--anchor", type=float, default=0.02, help="umap_spring 的锚定强度")
    _ARGS = ap.parse_args()

    B.project_3d = _patched_project
    B.layout_semantic = _patched_layout
    sys.argv = ["build", "--force",        # A/B 一律 --force：门是给上线产物把关的
                "--out-frontend", "/tmp/ab_out", "--out-agent", "/tmp/ab_agent"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            B.main()
        except SystemExit:
            pass
    fid = rho = fid_raw = "?"
    for ln in buf.getvalue().split("\n"):
        if "10-NN vs 处理空间 10-NN)" in ln:
            fid = ln.split("=")[1].split("（")[0].strip()
        if "参考口径" in ln:
            fid_raw = ln.split("=")[-1].strip()
        if "线长-相似度秩相关" in ln and "未达" not in ln:
            rho = ln.split("=")[1].split("（")[0].strip()
    print(f"{_ARGS.variant:14} 保真度(10-NN) {fid:>6}   vs 原始 embedding {fid_raw:>6}   rho {rho:>7}")


if __name__ == "__main__":
    main()
