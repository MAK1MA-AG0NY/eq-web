"""
fr_core.py — 耳机频响曲线加载 / 插值 / 平滑 / 目标合成 / EQ 拟合 核心库

设计目标: 把"用耳机 A 模拟耳机 B 的听感"这件事拆成可验证的数学步骤。
不依赖 AutoEq 包 (其 4.1.2 要求 Python <3.12), 只依赖 numpy + scipy。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import least_squares
from scipy.signal import firwin2, minimum_phase

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
F_MIN, F_MAX = 20.0, 20000.0
# 标准对数采样点 (每倍频程 96 点, 与 AutoEq 默认 f_res 思路一致)
DEFAULT_F_RES = 96


def log_frequencies(f_min: float = F_MIN, f_max: float = F_MAX,
                    f_res: int = DEFAULT_F_RES) -> np.ndarray:
    """生成对数等间隔频点。f_res = 每倍频程点数。"""
    n = int(np.round(np.log2(f_max / f_min) * f_res)) + 1
    return np.logspace(np.log10(f_min), np.log10(f_max), n)


# --------------------------------------------------------------------------
# 数据加载
# --------------------------------------------------------------------------
_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def parse_fr_text(text: str) -> tuple[np.ndarray, np.ndarray]:
    """
    容错解析频响文本。支持:
      - AutoEq / oratory1990 CSV:  "frequency,raw"
      - squig.link 导出:           "freq  amplitude" 或逗号/制表符分隔
      - 带注释行 (#, //) 的文件
    返回 (freq, gain) 升序数组。
    """
    freqs: list[float] = []
    gains: list[float] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line[0] in "#;/":            # 注释 / 空行
            continue
        if re.search(r"[A-Za-z]", line) and not _NUM.match(line):
            continue                                 # 纯表头行
        nums = [float(x) for x in _NUM.findall(line)]
        if len(nums) < 2:
            continue
        f, g = nums[0], nums[1]
        if f <= 0 or not np.isfinite(f) or not np.isfinite(g):
            continue
        freqs.append(f)
        gains.append(g)

    if len(freqs) < 4:
        raise ValueError(f"解析出的数据点太少 ({len(freqs)}), 请检查文件格式")

    f = np.asarray(freqs, dtype=float)
    g = np.asarray(gains, dtype=float)
    order = np.argsort(f)
    f, g = f[order], g[order]

    # 去重 (同频点取平均), 保证单调递增以便插值
    f_u, idx = np.unique(f, return_index=True)
    if len(f_u) != len(f):
        g = np.array([g[f == x].mean() for x in f_u])
    return f_u, g


def load_fr(path: str) -> tuple[np.ndarray, np.ndarray]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_fr_text(fh.read())


# --------------------------------------------------------------------------
# 频响对象
# --------------------------------------------------------------------------
@dataclass
class FrequencyResponse:
    """插值到标准对数网格上的频响曲线。"""
    name: str
    freq: np.ndarray
    gain: np.ndarray

    # ---- 构造 ----
    @classmethod
    def from_arrays(cls, name: str, freq: Sequence[float], gain: Sequence[float],
                    f_res: int = DEFAULT_F_RES, f_min=F_MIN, f_max=F_MAX
                    ) -> "FrequencyResponse":
        grid = log_frequencies(f_min, f_max, f_res)
        src_f = np.asarray(freq, float)
        src_g = np.asarray(gain, float)

        # 超出测量范围的部分用端点值外推, 避免插值产生 NaN
        lo, hi = src_f[0], src_f[-1]
        g = np.interp(grid, src_f, src_g)          # 先线性拿到基础值
        # 测量范围内用 PCHIP 保形插值, 减少过冲
        inside = (grid >= lo) & (grid <= hi)
        if inside.sum() > 1:
            g[inside] = PchipInterpolator(src_f, src_g)(grid[inside])
        return cls(name=name, freq=grid, gain=g)

    @classmethod
    def from_file(cls, path: str, name: str | None = None, **kw) -> "FrequencyResponse":
        f, g = load_fr(path)
        return cls.from_arrays(name or path, f, g, **kw)

    # ---- 基本运算 ----
    def copy(self, name: str | None = None) -> "FrequencyResponse":
        return FrequencyResponse(name or self.name, self.freq.copy(), self.gain.copy())

    def center(self) -> "FrequencyResponse":
        """整体归零: 以 100Hz~10kHz 的加权均值为 0 dB 基准。"""
        m = (self.freq >= 100) & (self.freq <= 10000)
        self.gain = self.gain - self.gain[m].mean()
        return self

    def smooth(self, window_oct: float = 1 / 6) -> "FrequencyResponse":
        """
        分数倍频程平滑 (对数域滑动平均)。
        1/6 倍频程 ≈ 人耳临界带宽度, 是耳机补偿的常用默认值。
        """
        n = max(3, int(round(window_oct * DEFAULT_F_RES)) | 1)   # 强制奇数
        if n >= len(self.gain):
            return self
        kernel = np.ones(n) / n
        pad = n // 2
        padded = np.concatenate([np.full(pad, self.gain[0]),
                                 self.gain,
                                 np.full(pad, self.gain[-1])])
        self.gain = np.convolve(padded, kernel, mode="valid")
        return self

    def resample(self, freq: np.ndarray) -> "FrequencyResponse":
        """重采样到指定频点网格。"""
        f = np.asarray(freq, dtype=float)
        return FrequencyResponse(self.name, f, np.interp(f, self.freq, self.gain))

    def __sub__(self, other: "FrequencyResponse") -> "FrequencyResponse":
        """
        曲线相减 (self - other), 语义与普通减法一致。

        注意: "把耳机 A 变成耳机 B 所需的 EQ" = B - A (目标减源),
        即 self=B, other=A。调用方向写反会让 EQ 完全反向。
        """
        if not np.allclose(self.freq, other.freq):
            other = other.resample(self.freq)
        return FrequencyResponse(f"{self.name} - {other.name}",
                                 self.freq.copy(), self.gain - other.gain)


# --------------------------------------------------------------------------
# 滤波器
# --------------------------------------------------------------------------
@dataclass
class Biquad:
    """RBJ Audio EQ Cookbook 双二阶滤波器。"""
    kind: str          # 'PEAK' | 'LOW_SHELF' | 'HIGH_SHELF' | 'LP' | 'HP'
    fc: float
    q: float
    gain: float = 0.0

    def response(self, freq: np.ndarray, fs: float = 48000.0) -> np.ndarray:
        """返回该滤波器在给定频点的复数响应。"""
        w0 = 2 * np.pi * self.fc / fs
        cw, sw = np.cos(w0), np.sin(w0)
        alpha = sw / (2 * self.q)
        A = 10 ** (self.gain / 40)          # 幅度用 A, 增益单位 dB

        if self.kind == "PEAK":
            b = [1 + alpha * A, -2 * cw, 1 - alpha * A]
            a = [1 + alpha / A, -2 * cw, 1 - alpha / A]
        elif self.kind == "LOW_SHELF":
            sq = 2 * np.sqrt(A) * alpha
            b = [A * ((A + 1) - (A - 1) * cw + sq),
                 2 * A * ((A - 1) - (A + 1) * cw),
                 A * ((A + 1) - (A - 1) * cw - sq)]
            a = [(A + 1) + (A - 1) * cw + sq,
                 -2 * ((A - 1) + (A + 1) * cw),
                 (A + 1) + (A - 1) * cw - sq]
        elif self.kind == "HIGH_SHELF":
            sq = 2 * np.sqrt(A) * alpha
            b = [A * ((A + 1) + (A - 1) * cw + sq),
                 -2 * A * ((A - 1) + (A + 1) * cw),
                 A * ((A + 1) + (A - 1) * cw - sq)]
            a = [(A + 1) - (A - 1) * cw + sq,
                 2 * ((A - 1) - (A + 1) * cw),
                 (A + 1) - (A - 1) * cw - sq]
        elif self.kind == "LP":
            b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
            a = [1 + alpha, -2 * cw, 1 - alpha]
        elif self.kind == "HP":
            b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
            a = [1 + alpha, -2 * cw, 1 - alpha]
        else:
            raise ValueError(f"未知滤波器类型: {self.kind}")

        b = np.asarray(b) / a[0]
        a = np.asarray(a) / a[0]
        z = np.exp(-1j * 2 * np.pi * np.asarray(freq) / fs)
        num = b[0] + b[1] * z + b[2] * z ** 2
        den = a[0] + a[1] * z + a[2] * z ** 2
        return num / den


def filters_response(filters: Iterable[Biquad], freq: np.ndarray,
                     fs: float = 48000.0) -> np.ndarray:
    """多个滤波器级联后的总增益 (dB)。"""
    total = np.ones(len(freq), dtype=complex)
    for filt in filters:
        total *= filt.response(freq, fs)
    return 20 * np.log10(np.maximum(np.abs(total), 1e-12))


# --------------------------------------------------------------------------
# 参数化 EQ 拟合
# --------------------------------------------------------------------------
# (kind, fc_min, fc_max, q_min, q_max, gain_min, gain_max)
FilterSpec = tuple

PEQ_CONFIGS: dict[str, list[FilterSpec]] = {
    # 10 段: 2 个搁架 + 8 个峰值, 通用耳机的甜点配置
    "10_BAND": (
        [("LOW_SHELF", 20, 200, 0.3, 1.2, -18, 18)] +
        [("PEAK", 40, 10000, 0.3, 6.0, -18, 18)] * 8 +
        [("HIGH_SHELF", 2000, 12000, 0.3, 1.2, -18, 18)]
    ),
    # 20 段: 和毁HIFI 段数对齐, 便于横向对比
    "20_BAND": (
        [("LOW_SHELF", 20, 200, 0.3, 1.2, -18, 18)] +
        [("PEAK", 30, 16000, 0.3, 8.0, -18, 18)] * 18 +
        [("HIGH_SHELF", 2000, 14000, 0.3, 1.2, -18, 18)]
    ),
    # 31 段: 桌面端激进配置, 高频分辨率更高
    "31_BAND": (
        [("LOW_SHELF", 20, 200, 0.3, 1.2, -18, 18)] +
        [("PEAK", 25, 18000, 0.3, 8.0, -18, 18)] * 29 +
        [("HIGH_SHELF", 2000, 16000, 0.3, 1.2, -18, 18)]
    ),
}


def _pack(specs: Sequence[FilterSpec], params: np.ndarray) -> list[Biquad]:
    out = []
    for spec, (fc, q, gain) in zip(specs, params.reshape(-1, 3)):
        out.append(Biquad(spec[0], float(fc), float(q), float(gain)))
    return out


def _bounds(specs: Sequence[FilterSpec]) -> tuple[np.ndarray, np.ndarray]:
    lo, hi = [], []
    for kind, fc0, fc1, q0, q1, g0, g1 in specs:
        lo += [fc0, q0, g0]
        hi += [fc1, q1, g1]
    return np.asarray(lo, float), np.asarray(hi, float)


def _filters_db_fast(specs: Sequence[FilterSpec], params: np.ndarray,
                     freq: np.ndarray, fs: float = 48000.0) -> np.ndarray:
    """
    向量化计算所有滤波器在 freq 上的总增益 (dB)。
    比逐个构造 Biquad 再调用 response() 快 1~2 个数量级, 是拟合能跑动的关键。
    params: (n_filters, 3) 的 [fc, q, gain]
    """
    n = len(specs)
    p = params.reshape(n, 3)
    fc_arr = p[:, 0][:, None]                 # (n,1)
    q_arr = p[:, 1][:, None]
    g_arr = p[:, 2][:, None]

    w0 = 2 * np.pi * fc_arr / fs              # (n,1)
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2 * q_arr)
    A = 10 ** (g_arr / 40)

    F = np.asarray(freq)[None, :]             # (1,m)
    z = np.exp(-1j * 2 * np.pi * F / fs)
    z2 = z * z

    total = np.ones((n, len(freq)), dtype=complex)
    kinds = [s[0] for s in specs]
    for i, kind in enumerate(kinds):
        if kind == "PEAK":
            b1 = 1 + alpha[i, 0] * A[i, 0]
            b2 = -2 * cw[i, 0]
            b3 = 1 - alpha[i, 0] * A[i, 0]
            a1 = 1 + alpha[i, 0] / A[i, 0]
            a2 = -2 * cw[i, 0]
            a3 = 1 - alpha[i, 0] / A[i, 0]
        elif kind == "LOW_SHELF":
            sq = 2 * np.sqrt(A[i, 0]) * alpha[i, 0]
            Am, Ap = A[i, 0], A[i, 0]
            c = cw[i, 0]
            b1 = Ap * ((Ap + 1) - (Ap - 1) * c + sq)
            b2 = 2 * Ap * ((Ap - 1) - (Ap + 1) * c)
            b3 = Ap * ((Ap + 1) - (Ap - 1) * c - sq)
            a1 = (Am + 1) + (Am - 1) * c + sq
            a2 = -2 * ((Am - 1) + (Am + 1) * c)
            a3 = (Am + 1) + (Am - 1) * c - sq
        elif kind == "HIGH_SHELF":
            sq = 2 * np.sqrt(A[i, 0]) * alpha[i, 0]
            c = cw[i, 0]
            b1 = A[i, 0] * ((A[i, 0] + 1) + (A[i, 0] - 1) * c + sq)
            b2 = -2 * A[i, 0] * ((A[i, 0] - 1) + (A[i, 0] + 1) * c)
            b3 = A[i, 0] * ((A[i, 0] + 1) + (A[i, 0] - 1) * c - sq)
            a1 = (A[i, 0] + 1) - (A[i, 0] - 1) * c + sq
            a2 = 2 * ((A[i, 0] - 1) - (A[i, 0] + 1) * c)
            a3 = (A[i, 0] + 1) - (A[i, 0] - 1) * c - sq
        else:
            raise ValueError(kind)

        num = b1 + b2 * z + b3 * z2
        den = a1 + a2 * z + a3 * z2
        total[i] = num / den

    mag = np.abs(total)
    mag[mag < 1e-12] = 1e-12
    return 20 * np.log10(mag).sum(axis=0)


def optimize_peq(target_db: np.ndarray, freq: np.ndarray,
                 config: str | Sequence[FilterSpec] = "10_BAND",
                 fs: float = 48000.0,
                 max_gain: float = 15.0,
                 f_weight_lo: float = 20.0,
                 f_weight_hi: float = 10000.0,
                 seed: int = 0,
                 n_restarts: int = 6,
                 f_res: int = 96,
                 reg_gain: float = 0.0) -> tuple[list[Biquad], np.ndarray]:
    """
    用最小二乘拟合参数化 EQ 去逼近 target_db。

    关键点:
      * 在对数频率域等权拟合 (感知上更合理), 而不是线性频率
      * >10kHz 降低权重: 该频段个体差异/测量误差极大, 强行拟合没有意义
      * 多起点重启: 避免陷入局部最优 (这是 AutoEq 也采用的策略)
    """
    if isinstance(config, str):
        specs = PEQ_CONFIGS[config]
    else:
        specs = list(config)

    n = len(specs)
    lo, hi = _bounds(specs)
    hi = hi.copy()
    hi[2::3] = np.minimum(hi[2::3], max_gain)
    lo[2::3] = np.maximum(lo[2::3], -max_gain)

    # 拟合用的降采样网格: 958 点 -> ~192 点, 速度提升 5x, 精度几乎无损
    grid = log_frequencies(max(F_MIN, freq[0]), min(F_MAX, freq[-1]), f_res=max(12, f_res // 5))
    fit_target = np.interp(grid, freq, target_db)

    # 感知权重: >10kHz 降低权重 (个体差异与测量误差大)
    w = np.ones_like(grid)
    w[grid > f_weight_hi] = 0.3
    w[grid < 25] = 0.5
    w = w * np.log2(grid)          # 对数频率下等感知权重
    w = w / w.max()

    def residual(p: np.ndarray) -> np.ndarray:
        model = _filters_db_fast(specs, p, grid, fs)
        err = (model - fit_target) * w
        if reg_gain > 0:
            # 用 L1 (惩罚 |gain|) 而不是 L2 (惩罚 gain^2) 正则化。
            # 原因: L2 会把一个大增益拆成同一频率上的多个小增益来降低代价
            # (g1^2+g2^2 < (g1+g2)^2), 导致输出一堆"叠罗汉"的同频滤波器。
            # L1 对同号叠加是中性的 (|g1|+|g2| == |g1+g2|), 只惩罚总增益大小,
            # 同时依然能抑制"正负增益互相抵消"的脆弱解。
            g = p.reshape(n, 3)[:, 2]
            err = np.concatenate([err, np.sqrt(reg_gain) * np.sqrt(np.abs(g) + 1e-6)])
        return err

    # ---- 智能初始化: 用峰值位置给每个滤波器一个合理起点 ----
    def initial_guess(rng) -> np.ndarray:
        # 找出目标曲线上幅度最大的区域作为滤波器中心频率候选
        idx = np.argsort(-np.abs(fit_target))
        cand = grid[idx[: max(n * 3, 12)]]
        # 去掉过近的候选
        chosen: list[float] = []
        for c in cand:
            if all(abs(np.log2(c / x)) > 0.25 for x in chosen):
                chosen.append(float(c))
            if len(chosen) >= n:
                break
        while len(chosen) < n:                       # 候补
            chosen.append(float(grid[rng.integers(len(grid))]))
        fc0 = np.array(sorted(chosen))
        g0 = np.interp(fc0, grid, fit_target) * 0.8  # 增益初值取目标值的一部分
        q0 = np.full(n, 1.2)
        return np.stack([fc0, q0, g0], axis=1).ravel()

    rng = np.random.default_rng(seed)
    best_p, best_cost = None, np.inf

    for attempt in range(n_restarts):
        x0 = initial_guess(rng)
        # 展平滤波器顺序: 按频率排序后, 峰值滤波器需保持频率递增 (便于后续可视化)
        if attempt > 0:
            x0 = x0.copy()
            x0[0::3] *= np.exp(rng.normal(0, 0.25, n))
            x0[1::3] = np.exp(rng.normal(np.log(1.2), 0.5, n))
        x0 = np.clip(x0, lo, hi)

        try:
            res = least_squares(residual, x0, bounds=(lo, hi),
                                method="trf", max_nfev=800,
                                xtol=1e-9, ftol=1e-9, gtol=1e-9)
        except Exception:
            continue
        if res.cost < best_cost:
            best_cost, best_p = res.cost, res.x

    if best_p is None:
        raise RuntimeError("PEQ 拟合失败")

    filters = _pack(specs, best_p)
    # 最终在完整网格上评估
    model = filters_response(filters, freq, fs)
    return filters, model


def merge_coincident(filters: Sequence[Biquad],
                     tol_oct: float = 0.15) -> list[Biquad]:
    """
    合并"同频叠加"的滤波器。

    最小二乘喜欢把一个大增益拆成落在同一频率上的多个滤波器 (例如 3 个都顶在
    20Hz 边界的峰值滤波器, 各 +3dB)。它们等价于一个 +9dB 的滤波器, 却白占段数,
    还让用户手动微调时无从下手。同频同类型的滤波器可以直接把增益相加合并。

    只合并**同号**增益: 同号叠加才近似等于增益相加; 异号的同频滤波器是
    "互相抵消"的脆弱解, 应当移除而不是合并 (由 prune 阶段处理)。

    tol_oct: 认为"同频"的对数频率容差 (0.15 octave ≈ 11%)
    """
    out: list[Biquad] = []
    for f in sorted(filters, key=lambda x: x.fc):
        if (out and f.kind == out[-1].kind
                and abs(np.log2(f.fc / out[-1].fc)) < tol_oct
                and (f.gain >= 0) == (out[-1].gain >= 0)):
            m = out[-1]
            m.gain += f.gain
            m.q = max(m.q, f.q)
        else:
            out.append(Biquad(f.kind, f.fc, f.q, f.gain))
    return out


def prune_and_refine(filters: list[Biquad], target_db: np.ndarray,
                     freq: np.ndarray, fs: float = 48000.0,
                     max_gain: float = 15.0,
                     n_restarts: int = 3,
                     min_contribution: float = 1.0,
                     reg_gain: float = 0.05
                     ) -> tuple[list[Biquad], np.ndarray]:
    """
    清理"互相抵消"的冗余滤波器并重新拟合。

    问题: 最小二乘可能解出两个中心频率相近、增益相反的滤波器
    (如 472Hz -6.18dB 与 466Hz +6.63dB), 净效果接近 0, 却白占两段。
    这在 20/31 段配置下尤其浪费。

    做法: 逐个移除"净贡献 < min_contribution dB"的滤波器,
    用剩下的段数重新优化, 若误差未变差则接受精简结果。
    """
    def rms_err(fs_: list[Biquad]) -> float:
        m = filters_response(fs_, freq, fs)
        w = (freq >= 20) & (freq <= 10000)
        return float(np.sqrt(np.mean((m[w] - target_db[w]) ** 2)))

    base_err = rms_err(filters)
    current = list(filters)

    # 迭代移除低贡献滤波器
    changed = True
    while changed and len(current) > 2:
        changed = False
        contrib = []
        for i, f in enumerate(current):
            others = current[:i] + current[i + 1:]
            full = filters_response(current, freq, fs)
            without = filters_response(others, freq, fs)
            contrib.append((float(np.sqrt(np.mean((full - without) ** 2))), i))
        contrib.sort()
        rms_contrib, idx = contrib[0]
        if rms_contrib >= min_contribution:
            break
        trial = current[:idx] + current[idx + 1:]
        if rms_err(trial) <= base_err * 1.05 + 0.05:   # 误差几乎不变则接受精简
            current = trial
            changed = True

    # 合并同频叠加的滤波器 (如 3 个都顶在 20Hz 边界的峰值滤波器)。
    # 同频同类型直接相加增益, 属近似操作, 因此仍需通过误差检查。
    merged = merge_coincident(current)
    if len(merged) < len(current) and rms_err(merged) <= base_err * 1.05 + 0.05:
        current = merged

    # 若确实精简了, 用剩余段数重新优化以填补空缺
    result = current
    if len(current) < len(filters):
        specs: list[FilterSpec] = []
        for f in current:
            lo_fc, hi_fc = f.fc * 0.5, f.fc * 2.0
            specs.append((f.kind, max(20, lo_fc), min(18000, hi_fc),
                          0.3, 8.0, -18, 18))
        try:
            refined, _ = optimize_peq(target_db, freq, config=specs, fs=fs,
                                      max_gain=max_gain, n_restarts=n_restarts,
                                      reg_gain=reg_gain)
            if rms_err(refined) <= base_err:
                result = refined
        except Exception:
            pass

    # 重拟合可能再次产生同频堆叠的滤波器, 因此在最后再合并一次
    merged = merge_coincident(result)
    if (len(merged) < len(result)
            and rms_err(merged) <= rms_err(result) * 1.05 + 0.05):
        result = merged

    return result, filters_response(result, freq, fs)


# --------------------------------------------------------------------------
# 卷积 EQ 导出
# --------------------------------------------------------------------------
def design_fir(target_db: np.ndarray, freq: np.ndarray, n_taps: int = 8192,
               fs: float = 48000.0, phase: str = "minimum"
               ) -> np.ndarray:
    """
    由目标增益曲线设计 FIR 冲激响应 (Equalizer APO 的 Convolution 可直接加载)。

    phase='minimum' -> 最小相位, 无预振铃, 听感更自然 (推荐)
    phase='linear'  -> 线性相位, 瞬态对称但引入延迟
    """
    # firwin2 的 Type I 滤波器要求奇数抽头; 偶数抽头是 Type II,
    # 会在 Nyquist 处强制零增益 -> 报 ValueError。这里强制奇数。
    n_taps = int(n_taps) | 1

    grid = np.linspace(0, fs / 2, n_taps // 2 + 1)
    # 归一化幅度到线性域; 超出数据范围用端点值
    mag_db = np.interp(grid, freq, target_db, left=target_db[0], right=target_db[-1])
    # 0 dB 对应 1.0; FIR 必须 <1 以免削波, 预留 headroom
    over = mag_db.max()
    if over > 0:
        mag_db = mag_db - over
    mag = 10 ** (mag_db / 20)

    taps = firwin2(n_taps, grid / (fs / 2), mag, window="hann")
    if phase == "minimum":
        taps = minimum_phase(taps, method="homomorphic")
    return taps.astype(np.float32)


def write_convolution_wav(path: str, taps: np.ndarray, fs: int = 48000) -> None:
    """写成 32-bit float WAV (Equalizer APO 卷积滤波器格式)。"""
    import struct
    import wave

    n = len(taps)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(4)          # 32-bit
        w.setframerate(fs)
        data = b"".join(struct.pack("<f", float(t)) for t in taps)
        w.writeframes(data)
        # 覆写 format tag 为 IEEE float (3), wave 模块写的是 1(PCM)
        w.close()

    with open(path, "r+b") as fh:
        fh.seek(20)
        fh.write(struct.pack("<H", 3))


# --------------------------------------------------------------------------
# 导出格式
# --------------------------------------------------------------------------
def to_equalizer_apo(filters: Sequence[Biquad], preamp_db: float) -> str:
    """生成 Equalizer APO 的 config.txt 片段。"""
    lines = [f"Preamp: {preamp_db:.2f} dB"]
    for i, f in enumerate(filters, 1):
        kind = {"PEAK": "PK", "LOW_SHELF": "LSC", "HIGH_SHELF": "HSC"}[f.kind]
        lines.append(f"Filter {i}: ON {kind} Fc {f.fc:.1f} Hz "
                     f"Gain {f.gain:.2f} dB Q {f.q:.2f}")
    return "\n".join(lines)


def to_peace(filters: Sequence[Biquad], preamp_db: float) -> str:
    """生成 Peace GUI 可导入的文本格式。"""
    lines = [f"Preamp: {preamp_db:.2f} dB"]
    for i, f in enumerate(filters, 1):
        kind = {"PEAK": "PK", "LOW_SHELF": "LSC", "HIGH_SHELF": "HSC"}[f.kind]
        lines.append(f"Filter {i}: ON {kind} Fc {f.fc:.1f} Hz "
                     f"Gain {f.gain:.2f} dB Q {f.q:.2f}")
    return "\n".join(lines)


def to_rew(filters: Sequence[Biquad]) -> str:
    """生成 Room EQ Wizard 可粘贴的滤波器列表。"""
    lines = []
    for f in filters:
        if f.kind == "PEAK":
            lines.append(f"PK       {f.fc:.1f}  {f.gain:.2f}  {f.q:.2f}")
        elif f.kind == "LOW_SHELF":
            lines.append(f"LSC      {f.fc:.1f}  {f.gain:.2f}  {f.q:.2f}")
        elif f.kind == "HIGH_SHELF":
            lines.append(f"HSC      {f.fc:.1f}  {f.gain:.2f}  {f.q:.2f}")
    return "\n".join(lines)


def to_autoeq_txt(filters: Sequence[Biquad], preamp_db: float,
                  name: str = "") -> str:
    """AutoEq 风格的 ParametricEQ.txt。"""
    lines = []
    if name:
        lines.append(f"# {name}")
    lines.append(f"Preamp: {preamp_db:.2f} dB")
    for i, f in enumerate(filters, 1):
        kind = {"PEAK": "PK", "LOW_SHELF": "LSC", "HIGH_SHELF": "HSC"}[f.kind]
        lines.append(f"Filter {i}: ON {kind} Fc {f.fc:.0f} Hz "
                     f"Gain {f.gain:.2f} dB Q {f.q:.2f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# PipeWire filter-chain 集成
# --------------------------------------------------------------------------
PW_LABEL = {"PEAK": "bq_peaking",
            "LOW_SHELF": "bq_lowshelf",
            "HIGH_SHELF": "bq_highshelf"}

# eq-web 面板的校验约束 (与 server.py 的 MIN_BANDS/MAX_BANDS/MIN_Q/... 保持一致)。
# 注意: 这些**不是 PipeWire 的限制** —— filter-chain 实测可加载 256 段 / Q=100。
# 面板的段数上限只受 suggest_preamp() 的计算耗时约束 (128 段≈4s)。
EQWEB_LIMITS = {"bands": (1, 128), "freq": (10.0, 22000.0),
                "q": (0.1, 50.0), "gain": (-24.0, 24.0),
                "preamp": (-24.0, 0.0)}


def worst_case_boost(filters: Sequence[Biquad],
                     fs_list: Sequence[float] = (44100.0, 48000.0),
                     f_lo: float = 10.0, f_hi: float = 20000.0) -> float:
    """
    计算级联滤波器在给定采样率下可能出现的最大正向增益 (dB)。

    preamp 必须按"最坏情况"取, 否则会削波。两个要点:
      * 不能只看设计采样率: 双二阶的响应随 fs 变化, 44.1k 与 48k 都要算;
      * 下探到 10Hz: 滤波器在 20Hz 以下依然有增益, 而重采样/低频素材会碰到它。
    """
    worst = 0.0
    grid = np.logspace(np.log10(f_lo), np.log10(f_hi), 4000)
    for fs in fs_list:
        grid_fs = grid[grid <= fs * 0.45]
        if not len(grid_fs):
            continue
        m = filters_response(filters, grid_fs, fs)
        worst = max(worst, float(np.max(m)))
    return worst


def to_pipewire_conf(description: str, stem: str, preamp_db: float,
                     filters: Sequence[Biquad],
                     panel_url: str = "http://127.0.0.1:8787") -> str:
    """
    生成 PipeWire filter-chain 配置 (drop-in 到 filter-chain.conf.d/)。

    格式与 eq-web 面板的 _render_conf() 完全一致, 因此面板会自动发现并管理它。
    节点名用 effect_input.{stem} / effect_output.{stem}, 面板靠前者识别虚拟声卡。
    """
    n = len(filters)
    mult = f"{round(10.0 ** (preamp_db / 20.0), 4):.4f}"
    lines = [
        f"# {description} · {n} 段参数均衡 (PEQ)",
        "#",
        "# 位置: ~/.config/pipewire/filter-chain.conf.d/",
        "#       由 filter-chain.service 自动加载，无需手动安装",
        "#",
        f"#   - {n} 个滤波器串联一阶",
        f"#   - 末尾 linear(pre) 提供 {preamp_db:g} dB 预增益（Mult = {mult}），保证不削波",
        "#   - 修改任何参数后执行: systemctl --user restart filter-chain.service",
        f"#   - Web 控制面板: {panel_url}",
        "",
        "context.modules = [",
        "  { name = libpipewire-module-filter-chain",
        "    args = {",
        f'      node.description = "{description}"',
        f'      media.name       = "{description}"',
        "      filter.graph = {",
        "        nodes = [",
    ]
    for i, f in enumerate(filters, 1):
        lines.append(
            f'          {{ type = builtin label = {PW_LABEL[f.kind]} name = f{i:02d} '
            f'control = {{ "Freq" = {f.fc:.1f}  "Q" = {f.q:.3f} '
            f'"Gain" = {f.gain: .3f} }} }}'
        )
    lines.append(
        f'          {{ type = builtin label = linear     name = pre '
        f'control = {{ "Mult" = {mult} }} }}'
    )
    lines.append("        ]")
    lines.append("        links = [")
    for i in range(1, n):
        lines.append(f'          {{ output = "f{i:02d}:Out" input = "f{i + 1:02d}:In" }}')
    lines.append(f'          {{ output = "f{n:02d}:Out" input = "pre:In" }}')
    lines += [
        "        ]",
        "      }",
        "      capture.props = {",
        f'        node.name      = "effect_input.{stem}"',
        '        media.class    = Audio/Sink',
        "        audio.channels = 2",
        '        audio.position = [ FL FR ]',
        "      }",
        "      playback.props = {",
        f'        node.name      = "effect_output.{stem}"',
        "        node.passive   = true",
        "        audio.channels = 2",
        '        audio.position = [ FL FR ]',
        "      }",
        "    }",
        "  }",
        "]",
        "",
    ]
    return "\n".join(lines)


def to_paste_text(filters: Sequence[Biquad]) -> str:
    """
    生成毁HiFi 导出格式的行, 可直接粘贴进 eq-web 面板的解析框。

    eq-web 的 _parse_filter_line 要求每行形如:
        类型=峰值 频率=5725Hz Gain=1.621549 Q=5
    只认 峰值 / 低架 / 高架 三种类型。
    """
    cn = {"PEAK": "峰值", "LOW_SHELF": "低架", "HIGH_SHELF": "高架"}
    return "\n".join(
        f"类型={cn[f.kind]} 频率={f.fc:.1f}Hz Gain={f.gain:.6f} Q={f.q:.3f}"
        for f in filters)


def eqweb_issues(filters: Sequence[Biquad], preamp_db: float) -> list[str]:
    """检查输出是否满足 eq-web 面板 /api/eq/create 的校验约束。"""
    issues: list[str] = []
    n = len(filters)
    lo, hi = EQWEB_LIMITS["bands"]
    if not (lo <= n <= hi):
        issues.append(f"段数 {n} 超出面板允许的 {lo}~{hi} 段 "
                      f"(conf 仍可用, 但无法在面板里编辑/新建)")
    for i, f in enumerate(filters, 1):
        if not (EQWEB_LIMITS["freq"][0] <= f.fc <= EQWEB_LIMITS["freq"][1]):
            issues.append(f"f{i:02d} 频率 {f.fc:.1f}Hz 超出 10~22000Hz")
        if not (EQWEB_LIMITS["q"][0] <= f.q <= EQWEB_LIMITS["q"][1]):
            issues.append(f"f{i:02d} Q {f.q:.2f} 超出 0.1~20")
        if not (EQWEB_LIMITS["gain"][0] <= f.gain <= EQWEB_LIMITS["gain"][1]):
            issues.append(f"f{i:02d} Gain {f.gain:.2f} 超出 ±24dB")
    p_lo, p_hi = EQWEB_LIMITS["preamp"]
    if not (p_lo <= preamp_db <= p_hi):
        issues.append(f"Preamp {preamp_db:.2f}dB 超出 {p_lo}~{p_hi}dB")
    return issues


def slugify(text: str) -> str:
    """把任意描述转成安全的文件名 / 节点名片段。"""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:40] or "preset"