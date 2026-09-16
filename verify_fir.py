"""
verify_fir.py — 校验导出的卷积 FIR 是否真的逼近目标曲线

注意: Python 标准库 wave 模块不支持读取 IEEE float (format tag 3) 的 WAV,
而 Equalizer APO 的卷积滤波器要求正是 32-bit float。因此这里手写 RIFF 解析。
"""
import struct
import sys

import numpy as np


def read_float_wav(path: str):
    """手动解析 RIFF/WAVE, 返回 (taps, fs, fmt_tag, nch, bits)。"""
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是合法的 RIFF/WAVE 文件")

    pos = 12
    fmt = None
    samples = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csize = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + csize]
        if cid == b"fmt ":
            (tag, nch, fs, _bps, _balign, bits) = struct.unpack("<HHIIHH", body[:16])
            fmt = (tag, nch, fs, bits)
        elif cid == b"data":
            samples = body
        pos += 8 + csize + (csize & 1)          # 块按偶数字节对齐

    if fmt is None or samples is None:
        raise ValueError("缺少 fmt 或 data 块")

    tag, nch, fs, bits = fmt
    if bits != 32:
        raise ValueError(f"期望 32-bit, 实际 {bits}-bit")
    if tag != 3:
        raise ValueError(f"期望 IEEE float (tag=3), 实际 tag={tag}")

    n = len(samples) // 4
    taps = np.array(struct.unpack("<%df" % n, samples[:n * 4]), dtype=float)
    return taps, fs, tag, nch, bits


def main() -> int:
    path, target_csv = sys.argv[1], sys.argv[2]
    taps, fs, tag, nch, bits = read_float_wav(path)
    print(f"WAV 解析: {nch}ch {bits}bit fmt_tag={tag}(IEEE float) "
          f"fs={fs} taps={len(taps)}")
    print("-> 格式正确, Equalizer APO 可直接加载")

    # 频响: 补零到高分辨率再 FFT
    N = 1 << 17
    H = np.fft.rfft(taps, n=N)
    f = np.fft.rfftfreq(N, 1.0 / fs)
    mag = 20 * np.log10(np.abs(H) + 1e-15)

    tgt_f, tgt_g = [], []
    with open(target_csv) as fh:
        next(fh)
        for line in fh:
            a, b = line.strip().split(",")
            tgt_f.append(float(a))
            tgt_g.append(float(b))
    tgt_f = np.array(tgt_f)
    tgt_g = np.array(tgt_g)

    probe = np.array([50., 100., 200., 500., 1000., 2000., 4000., 8000.])
    print("\n   freq     FIR(dB)  target(dB)   diff(dB)")
    errs = []
    for p in probe:
        m = float(np.interp(p, f, mag))
        t = float(np.interp(p, tgt_f, tgt_g))
        errs.append(m - t)
        print(f"{p:8.0f} {m:9.2f} {t:11.2f} {m - t:10.2f}")
    errs = np.array(errs)

    # FIR 有意预留 headroom (整体负偏移), 用 1kHz 对齐后只评估"形状"
    ref = float(np.interp(1000., f, mag) - np.interp(1000., tgt_f, tgt_g))
    shape_err = errs - ref
    print(f"\n整体偏移 {ref:+.2f} dB (headroom 预留, 属正常)")
    print(f"对齐后形状误差: max={np.abs(shape_err).max():.2f} dB")
    ok_shape = np.abs(shape_err).max() < 2.0
    print("-> FIR 形状匹配通过" if ok_shape else "-> 警告: FIR 形状偏差偏大")

    # 最小相位检查: 能量应集中在前部 (无预振铃)
    e = np.cumsum(taps ** 2) / np.sum(taps ** 2)
    i1, i10 = int(len(taps) * .01), int(len(taps) * .1)
    print(f"能量累积: 前1%={e[i1]:.3f}  前10%={e[i10]:.3f}")
    ok_phase = e[i10] > 0.99
    print("-> 最小相位: 能量靠前, 无预振铃" if ok_phase
          else "-> 注意: 能量分散, 可能有预振铃")

    return 0 if (ok_shape and ok_phase) else 1


if __name__ == "__main__":
    sys.exit(main())
