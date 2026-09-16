"""sim.py — 耳机模拟: 测量数据库索引 + 频响缓存 + 调用 DSP 引擎

设计要点:
  * **同源优先**: 所有选择都先确定"测量源"(数据库/人工头), 两副耳机只能同源,
    从结构上杜绝"跨源拼凑"这个最容易犯且结果完全不可信的错。
  * 索引只需 1 次 GitHub API 调用 (只列 measurements 子树, 避开体积巨大的 results/)。
  * 频响 CSV 按需下载并缓存到 ~/.cache/eq-web/。
  * DSP 引擎复用本项目内的 fr_core.py (已经过与 PipeWire 逐项比对验证),
    避免重复实现。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# numpy/scipy 只有真正做 DSP 时才需要。索引/搜索/同源检查是纯标准库实现,
# 因此这里做成惰性依赖: 缺 numpy 时这些功能照常可用, 只有 simulate() 明确报错。
try:
    import numpy as _np
except ImportError:                                       # pragma: no cover
    _np = None

# DSP 引擎 fr_core.py 已并入本项目, 与本模块同目录, 直接 import 即可。
# 保留 EQ_WEB_SIM_DIR 只是为了把引擎放到别处时仍能工作 (可选)。
LOCAL_DIR = Path(__file__).resolve().parent
SIM_DIR = Path(os.path.expanduser(
    os.environ.get("EQ_WEB_SIM_DIR", str(LOCAL_DIR))))

CACHE_DIR = Path(os.path.expanduser("~/.cache/eq-web"))
INDEX_FILE = CACHE_DIR / "autoeq-index.json"
FR_DIR = CACHE_DIR / "fr"
INDEX_TTL = 7 * 24 * 3600          # 索引缓存 7 天

# 只列 measurements 子树: 整仓 tree 会被截断, 子树是完整的 (truncated=false)
TREE_URL = ("https://api.github.com/repos/jaakkopasanen/AutoEq/"
            "git/trees/master:measurements?recursive=1")
RAW_BASE = ("https://raw.githubusercontent.com/jaakkopasanen/AutoEq/"
            "master/measurements")

CATEGORY_CN = {"over-ear": "头戴式", "in-ear": "入耳式", "earbud": "平头塞"}

_index_lock = threading.Lock()
_index: dict | None = None
_engine = None


class SimError(Exception):
    """模拟相关的可预期错误 (会以 400 返回给前端)。"""


# ---------------------------------------------------------------- 通用下载

def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        # GitHub API 要求 User-Agent
        "User-Agent": "eq-web-sim/1.0",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------- 索引

def _build_index() -> dict:
    raw = _http_get(TREE_URL, timeout=90)
    data = json.loads(raw.decode("utf-8"))
    if data.get("truncated"):
        raise SimError("测量索引被截断, 请稍后重试")
    tree = {}
    for item in data.get("tree", []):
        path = item.get("path", "")
        if not path.endswith(".csv"):
            continue
        m = re.match(r"^([^/]+)/data/([^/]+)/(.+)\.csv$", path)
        if not m:
            continue
        source, category, name = m.group(1), m.group(2), m.group(3)
        tree.setdefault(source, {}).setdefault(category, []).append(name)
    for cats in tree.values():
        for names in cats.values():
            names.sort()
    return tree


def load_index(force: bool = False) -> dict:
    """读取索引 (内存 -> 磁盘缓存 -> 网络)。"""
    global _index
    with _index_lock:
        if _index is not None and not force:
            return _index
        if not force and INDEX_FILE.is_file():
            try:
                if time.time() - INDEX_FILE.stat().st_mtime < INDEX_TTL:
                    _index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
                    return _index
            except (OSError, json.JSONDecodeError):
                pass
        tree = _build_index()
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = INDEX_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(tree, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, INDEX_FILE)
        except OSError:
            pass                        # 缓存失败不影响功能
        _index = tree
        return _index


def list_sources() -> list[dict]:
    tree = load_index()
    out = []
    for source, cats in tree.items():
        total = sum(len(v) for v in cats.values())
        out.append({
            "name": source,
            "total": total,
            "categories": [
                {"key": c, "label": CATEGORY_CN.get(c, c), "count": len(v)}
                for c, v in sorted(cats.items())
            ],
        })
    out.sort(key=lambda x: (-x["total"], x["name"]))
    return out


def list_headphones(source: str) -> list[dict]:
    tree = load_index()
    if source not in tree:
        raise SimError(f"未知的测量源: {source}")
    out = []
    for category, names in sorted(tree[source].items()):
        for n in names:
            out.append({"name": n, "category": category,
                        "category_cn": CATEGORY_CN.get(category, category)})
    out.sort(key=lambda x: (x["category"], x["name"].lower()))
    return out


# ---------------------------------------------------------------- 找同源

def search_headphone(query: str, limit: int = 200) -> list[dict]:
    """在所有测量源里模糊查找某副耳机, 返回 (源, 类目, 名称)。

    这是"找同源"的核心: 同一副耳机可能被多个源测过, 结果彼此不可混用。
    """
    q = (query or "").strip().lower()
    if len(q) < 2:
        raise SimError("请至少输入 2 个字符")
    tree = load_index()
    hits = []
    for source, cats in tree.items():
        for category, names in cats.items():
            for n in names:
                if q in n.lower():
                    hits.append({
                        "source": source,
                        "category": category,
                        "category_cn": CATEGORY_CN.get(category, category),
                        "name": n,
                    })
    hits.sort(key=lambda x: (x["name"].lower(), x["source"]))
    return hits[:limit]


def common_sources(name_a: str, name_b: str) -> dict:
    """找出同时收录了两副耳机的测量源 —— 只有这些源才能安全互模拟。"""
    if not name_a or not name_b:
        raise SimError("请同时提供两副耳机")
    tree = load_index()
    a_l, b_l = name_a.strip().lower(), name_b.strip().lower()
    a_src, b_src = set(), set()
    for source, cats in tree.items():
        for names in cats.values():
            low = [n.lower() for n in names]
            if any(a_l in n for n in low):
                a_src.add(source)
            if any(b_l in n for n in low):
                b_src.add(source)
    common = sorted(a_src & b_src)
    return {
        "from_sources": sorted(a_src),
        "to_sources": sorted(b_src),
        "common": common,
        "safe": bool(common),
    }


# ---------------------------------------------------------------- FR 缓存

def _safe_stem(source: str, name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{source}__{name}")[:180]


def _resolve(source: str, name: str) -> tuple[str, str]:
    """在索引中定位耳机, 返回 (category, name)。

    索引里的名字是相对**类目目录**的 (如 over-ear/HIFIMAN HE400se),
    而且本身还可能带子目录 (如 over-ear/HMS II.3/HIFIMAN HE400se),
    因此必须把 category 一起拼进下载路径, 否则会 404。
    """
    tree = load_index()
    if source not in tree:
        raise SimError(f"未知的测量源: {source}")
    for category, names in sorted(tree[source].items()):
        if name in names:
            return category, name
    raise SimError(f"「{name}」不在测量源 {source} 中")


def fr_path(source: str, name: str, category: str | None = None) -> Path:
    """返回本地缓存的频响 CSV 路径, 必要时下载。"""
    if category is None:
        category, name = _resolve(source, name)
    else:
        tree = load_index()
        if category not in tree.get(source, {}) or name not in tree[source][category]:
            raise SimError(f"「{name}」不在 {source} / {category} 中")

    FR_DIR.mkdir(parents=True, exist_ok=True)
    dest = FR_DIR / f"{_safe_stem(source, f'{category}/{name}')}.csv"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest

    rel = f"{source}/data/{category}/{name}.csv"
    url = f"{RAW_BASE}/{urllib.parse.quote(rel)}"
    try:
        blob = _http_get(url, timeout=45)
    except urllib.error.HTTPError as e:
        raise SimError(f"下载频响失败 (HTTP {e.code}): {name}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise SimError(f"下载频响失败 (网络): {e}")
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- DSP 引擎

def engine():
    """惰性加载 fr_core (numpy/scipy 实现, 不污染启动开销)。

    fr_core.py 与本模块同目录 (已合并进 eq-web), 正常情况下直接 import。
    """
    global _engine
    if _engine is not None:
        return _engine
    if not (SIM_DIR / "fr_core.py").is_file():
        raise SimError(f"找不到模拟引擎 fr_core.py: {SIM_DIR / 'fr_core.py'}")
    if str(SIM_DIR) not in sys.path:
        sys.path.insert(0, str(SIM_DIR))
    try:
        import fr_core                                   # noqa: PLC0415
    except ImportError as e:
        raise SimError(f"加载模拟引擎失败 (需要 numpy/scipy): {e}")
    _engine = fr_core
    return _engine


# fr_core 的滤波器类型 -> eq-web 的类型名
_KIND_MAP = {"PEAK": "peaking", "LOW_SHELF": "lowshelf", "HIGH_SHELF": "highshelf"}


def simulate(source: str, name_from: str, name_to: str, bands: int = 10,
             treble_mode: str = "shelf", bass_boost: float = 0.0,
             smooth: float = 1 / 6, restarts: int = 1) -> dict:
    """
    计算"用 A 模拟 B"所需的参数 EQ。

    返回的 bands 使用 eq-web 的类型名, 可直接交给 /api/eq/create 安装。
    """
    fc = engine()
    np = _np
    if np is None:
        raise SimError("模拟需要 numpy/scipy, 当前 Python 环境没有安装。"
                       f"服务使用的解释器是 {sys.executable}")
    if bands < 1 or bands > 120:
        raise SimError("段数需在 1~120 之间")

    t0 = time.time()
    path_a = fr_path(source, name_from)
    path_b = fr_path(source, name_to)

    try:
        fa = fc.FrequencyResponse.from_file(str(path_a), name_from)
        fb = fc.FrequencyResponse.from_file(str(path_b), name_to)
    except Exception as e:                                # noqa: BLE001
        raise SimError(f"解析频响失败: {e}")

    a = fa.copy().center().smooth(smooth)
    b = fb.copy().center().smooth(smooth)
    delta = b - a                                    # 方向: 目标 - 源

    # 高频策略: 10kHz 以上测量不可信, 默认改用平滑搁架
    f = delta.freq
    if treble_mode == "shelf":
        m = f >= 8000.0
        if m.any():
            hi = f >= 10000.0
            val = float(delta.gain[hi].mean()) if hi.any() else float(delta.gain[m].mean())
            t = np.clip(np.log2(f[m] / 8000.0) / 1.0, 0, 1)
            delta.gain[m] = delta.gain[m] * (1 - t) + val * (t * t * (3 - 2 * t))
    elif treble_mode == "ignore":
        m = f >= 10000.0
        if m.any():
            w = np.clip((f[m] - 10000.0) / 6000.0, 0, 1)
            delta.gain[m] = delta.gain[m] * (1 - w)

    if bass_boost:
        delta.gain += fc.filters_response(
            [fc.Biquad("LOW_SHELF", 105.0, 0.7, bass_boost)], f, 48000.0)

    # 任意段数配置: 1 低搁架 + N-2 峰值 + 1 高搁架
    if bands <= 2:
        specs = [("PEAK", 25, 18000, 0.3, 8.0, -18, 18)]
    else:
        specs = [("LOW_SHELF", 20, 200, 0.3, 1.2, -18, 18)]
        specs += [("PEAK", 25, 18000, 0.3, 8.0, -18, 18)] * (bands - 2)
        specs += [("HIGH_SHELF", 2000, 16000, 0.3, 1.2, -18, 18)]

    filters, model = fc.optimize_peq(
        delta.gain, delta.freq, config=specs, fs=48000.0,
        max_gain=15.0, reg_gain=0.05, n_restarts=restarts)
    filters, model = fc.prune_and_refine(
        filters, delta.gain, delta.freq, fs=48000.0,
        max_gain=15.0, reg_gain=0.05, n_restarts=min(restarts, 2))
    filters = sorted(filters, key=lambda x: x.fc)
    model = fc.filters_response(filters, delta.freq, 48000.0)

    # 预增益: 44.1k/48k 下的最坏峰值
    boost = fc.worst_case_boost(filters, fs_list=(44100.0, 48000.0))
    preamp = -boost if boost > 0 else 0.0

    # 真实模拟误差 (源+EQ vs 目标)
    emulated = a.gain + model
    metrics = {}
    for lo, hi, key in ((20, 1000, "low"), (1000, 10000, "mid"),
                        (10000, 20000, "high")):
        m = (delta.freq >= lo) & (delta.freq < hi)
        if m.any():
            e = emulated[m] - b.gain[m]
            metrics[key] = {
                "rms": round(float(np.sqrt(np.mean(e ** 2))), 3),
                "max": round(float(np.max(np.abs(e))), 3),
            }

    out_bands = [{
        "type": _KIND_MAP[x.kind],
        "freq": round(x.fc, 1),
        "q": round(x.q, 3),
        "gain": round(x.gain, 3),
    } for x in filters]

    return {
        "source": source,
        "from": name_from,
        "to": name_to,
        "bands": out_bands,
        "band_count": len(out_bands),
        "requested_bands": bands,
        "preamp_db": round(preamp, 2),
        "metrics": metrics,
        "elapsed": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------- 异步任务
# 拟合一次要 7~15 秒。server.py 的请求处理被 _REQ_LOCK 串行化,
# 若同步执行会把整个面板卡住, 因此这里放到后台线程, 前端轮询取结果。

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_job_seq = 0


def start_sim_job(**kwargs) -> str:
    """后台启动一次模拟, 立即返回任务 id。"""
    global _job_seq
    with _jobs_lock:
        _job_seq += 1
        jid = f"sim{_job_seq}"
        _jobs[jid] = {"id": jid, "state": "running", "started": time.time()}
        # 只保留最近 20 个任务, 避免无限增长
        if len(_jobs) > 20:
            for stale in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-20]:
                _jobs.pop(stale, None)

    def _run() -> None:
        try:
            res = simulate(**kwargs)
            with _jobs_lock:
                _jobs[jid].update(state="done", result=res)
        except SimError as e:
            with _jobs_lock:
                _jobs[jid].update(state="error", error=str(e))
        except Exception as e:                            # noqa: BLE001
            with _jobs_lock:
                _jobs[jid].update(state="error", error=f"模拟失败: {e}")

    threading.Thread(target=_run, daemon=True, name=f"sim-{jid}").start()
    return jid


def job_status(jid: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(jid)
        if job is None:
            raise SimError("任务不存在或已过期")
        out = {"id": jid, "state": job["state"],
               "elapsed": round(time.time() - job["started"], 1)}
        if job["state"] == "done":
            out["result"] = job["result"]
        elif job["state"] == "error":
            out["error"] = job["error"]
    return out


def warm_index() -> None:
    """预热索引 (首次需要一次网络请求), 供服务启动时后台调用。"""
    try:
        load_index()
    except Exception:                                     # noqa: BLE001
        pass
