#!/usr/bin/env python3
"""
hp_emulate.py — 用耳机 A 模拟耳机 B 的听感, 生成任意段数 EQ

用法示例:
  # 基础: 把 HD650 调成 HD600 的听感, 输出 10 段参数 EQ
  python3 hp_emulate.py --from "measurements/Sennheiser HD 650.csv" \
                        --to   "measurements/Sennheiser HD 600.csv" \
                        --bands 10 --outdir out

  # 桌面端暴力模式: 31 段 + 卷积 FIR
  python3 hp_emulate.py --from A.csv --to B.csv --bands 31 --convolution

  # 加上哈曼低频增益 (+6dB) 和亮度 tilt
  python3 hp_emulate.py --from A.csv --to B.csv --bands 20 --bass-boost 6 --tilt -0.5

关键参数:
  --smooth      平滑倍频程 (默认 1/6, 越大越保守)
  --max-gain    单个滤波器最大增益, 防止产生破坏性尖峰
  --treble-mode 高频处理策略: match(硬拟合) / shelf(整体搁架) / ignore(不动高频)
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys

import numpy as np

import fr_core as fc

# PipeWire filter-chain 的 drop-in 目录 (eq-web 面板扫描的同一个目录)
EQ_DIR = os.path.expanduser("~/.config/pipewire/filter-chain.conf.d")
SERVICE_UNIT = "filter-chain.service"


def build_target(src: fc.FrequencyResponse, dst: fc.FrequencyResponse,
                 args: argparse.Namespace) -> fc.FrequencyResponse:
    """
    核心: 计算 delta = dst - src, 这就是"把 src 变成 dst"所需的 EQ 曲线。
    所有曲线先归零再相减, 避免绝对声压级影响。
    """
    s = src.copy().center().smooth(args.smooth)
    d = dst.copy().center().smooth(args.smooth)

    # 对齐到同一网格
    d = fc.FrequencyResponse.from_arrays(d.name, d.freq, d.gain)
    s = fc.FrequencyResponse.from_arrays(s.name, s.freq, s.gain)

    delta = d - s
    delta.name = f"{dst.name} minus {src.name}"

    # --- 高频策略 ---
    f = delta.freq
    if args.treble_mode == "shelf":
        # 高频测量可信度低 (耳道共振个体差异大), 用整体搁架代替逐点拟合。
        # 必须平滑过渡: 硬切换会在 ~10kHz 形成阶跃 -> FIR 振铃/预回声。
        m = f >= args.treble_fc
        if m.any():
            hi = f >= 10000
            shelf_val = (float(delta.gain[hi].mean()) if hi.any()
                         else float(delta.gain[m].mean()))
            t = np.clip(np.log2(f[m] / args.treble_fc) / 1.0, 0, 1)
            t = t * t * (3 - 2 * t)          # smoothstep, 8000->16000Hz 过渡
            delta.gain[m] = delta.gain[m] * (1 - t) + shelf_val * t
    elif args.treble_mode == "ignore":
        m = f >= 10000
        if m.any():
            # 平滑地拉向 0, 不做高频修正
            w = np.clip((f[m] - 10000) / 6000, 0, 1)
            delta.gain[m] = delta.gain[m] * (1 - w)

    # --- 低频增益 (哈曼式) ---
    if args.bass_boost:
        fc_, q = 105.0, 0.7
        shelf = fc.filters_response(
            [fc.Biquad("LOW_SHELF", fc_, q, args.bass_boost)], f, args.fs)
        delta.gain += shelf

    # --- 整体 tilt (dB/octave, 负数=变暗) ---
    if args.tilt:
        delta.gain += args.tilt * np.log2(f / 1000.0)

    return delta


def sanitize_name(name: str) -> str:
    """与 eq-web 的 _sanitize_name 保持一致的清洗规则。"""
    s = (name or "").strip().replace("/", "")
    s = re.sub(r"\s+", "-", s)
    return s[:50].strip("-") or "preset"


def install_pipewire(conf_text: str, slug: str, restart: bool = True
                     ) -> tuple[str, str]:
    """
    把配置写入 filter-chain.conf.d/。

    若已存在同 slug 的配置则**原地更新**(便于反复迭代同一副耳机),
    否则按 eq-web 的编号约定取下一个空闲编号新建。
    返回 (路径, '新建'|'更新')。
    """
    os.makedirs(EQ_DIR, exist_ok=True)
    existing = sorted(glob.glob(os.path.join(EQ_DIR, "*.conf")))

    target, nums = None, []
    for p in existing:
        base = os.path.basename(p)
        m = re.match(r"^(\d+)-", base)
        if m:
            nums.append(int(m.group(1)))
        if base == f"{base.split('-')[0]}-{slug}.conf" or base.endswith(f"-{slug}.conf"):
            target = p

    if target is None:
        nn = (max(nums) + 1) if nums else 60
        target = os.path.join(EQ_DIR, f"{nn:02d}-{slug}.conf")
        action = "新建"
    else:
        action = "更新"

    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(conf_text)
    os.replace(tmp, target)          # 原子替换, 避免服务读到半个文件

    if restart:
        subprocess.run(["systemctl", "--user", "restart", SERVICE_UNIT],
                       check=False, capture_output=True, timeout=30)
    return target, action


def main() -> int:
    p = argparse.ArgumentParser(
        description="耳机听感模拟器: 用 A 模拟 B, 生成任意段数 EQ",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--from", dest="src", required=True, help="你的耳机 (被调整的) 频响文件")
    p.add_argument("--to", dest="dst", required=True, help="目标耳机 (要模拟的) 频响文件")
    p.add_argument("--bands", type=int, default=10, help="参数 EQ 段数 (默认 10; 家用可 20/31)")
    p.add_argument("--outdir", default="out", help="输出目录")
    p.add_argument("--fs", type=int, default=48000, help="采样率")
    p.add_argument("--smooth", type=float, default=1 / 6, help="平滑倍频程 (默认 1/6)")
    p.add_argument("--max-gain", type=float, default=15.0, help="单滤波器最大增益 dB")
    p.add_argument("--bass-boost", type=float, default=0.0, help="低频搁架增益 dB")
    p.add_argument("--tilt", type=float, default=0.0, help="整体倾斜 dB/octave")
    p.add_argument("--treble-mode", choices=["match", "shelf", "ignore"],
                   default="shelf", help="高频处理策略")
    p.add_argument("--treble-fc", type=float, default=8000.0,
                   help="高频搁架过渡起始频率 (默认 8000Hz)")
    p.add_argument("--reg", type=float, default=0.05,
                   help="增益正则化强度 (默认 0.05): 抑制互相抵消的大增益滤波器")
    p.add_argument("--convolution", action="store_true", help="额外导出卷积 FIR WAV")
    p.add_argument("--taps", type=int, default=8192, help="FIR 抽头数")
    p.add_argument("--phase", choices=["minimum", "linear"], default="minimum")
    p.add_argument("--plot", action="store_true", help="输出对比图 PNG")
    # ---- PipeWire / eq-web 集成 ----
    p.add_argument("--pipewire", nargs="?", const="", default=None, metavar="显示名",
                   help="安装为 PipeWire filter-chain 预设: 写入 "
                        "~/.config/pipewire/filter-chain.conf.d/ 并重启 filter-chain.service "
                        "(参数为可选的显示名, 留空则自动生成)")
    p.add_argument("--no-restart", action="store_true",
                   help="配合 --pipewire: 只写配置, 不重启 filter-chain.service")
    p.add_argument("--paste-text", action="store_true",
                   help="输出毁HiFi 格式文本 (可直接粘贴进 eq-web 面板)")
    args = p.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    # ---- 加载 ----
    try:
        src = fc.FrequencyResponse.from_file(args.src)
        dst = fc.FrequencyResponse.from_file(args.dst)
    except Exception as e:
        print(f"[错误] 读取频响失败: {e}", file=sys.stderr)
        return 1

    print(f"源耳机 : {src.name}")
    print(f"目标   : {dst.name}")

    # ---- 目标曲线 ----
    delta = build_target(src, dst, args)

    # ---- 拟合 ----
    cfg = {10: "10_BAND", 20: "20_BAND", 31: "31_BAND"}.get(args.bands)
    if cfg is None:
        # 任意段数: 动态构造 (1 低搁架 + N-2 峰值 + 1 高搁架)
        specs = [("LOW_SHELF", 20, 200, 0.3, 1.2, -18, 18)]
        specs += [("PEAK", 25, 18000, 0.3, 8.0, -18, 18)] * max(1, args.bands - 2)
        specs += [("HIGH_SHELF", 2000, 16000, 0.3, 1.2, -18, 18)]
        cfg = specs
        print(f"使用自定义 {args.bands} 段配置")

    print(f"拟合 {args.bands} 段参数 EQ ...")
    filters, model = fc.optimize_peq(
        delta.gain, delta.freq, config=cfg, fs=args.fs,
        max_gain=args.max_gain, reg_gain=args.reg)

    # 清理互相抵消的冗余段 (低段数时尤其重要)
    filters, model = fc.prune_and_refine(
        filters, delta.gain, delta.freq, fs=args.fs,
        max_gain=args.max_gain, reg_gain=args.reg)

    # 按频率排序, 便于阅读和手动微调
    filters = sorted(filters, key=lambda f: f.fc)
    model = fc.filters_response(filters, delta.freq, args.fs)

    if len(filters) < args.bands:
        print(f"  (已精简为 {len(filters)} 段有效滤波器, 移除冗余段)")

    # ---- 误差统计 ----
    w = (delta.freq >= 20) & (delta.freq <= 10000)
    err = model[w] - delta.gain[w]
    rms = float(np.sqrt(np.mean(err ** 2)))
    mx = float(np.max(np.abs(err)))

    print(f"\n拟合精度 (20Hz-10kHz):")
    print(f"  RMS 误差 : {rms:.2f} dB")
    print(f"  最大误差 : {mx:.2f} dB")
    print(f"  低频最大 : {float(np.max(np.abs(err[delta.freq[w] < 200]))):.2f} dB")

    # ---- 真实"模拟误差": 源耳机 + EQ 与目标耳机的实际差距 ----
    # 注意这与上面的"拟合误差"不同: 拟合误差只衡量 EQ 逼近所需曲线的程度,
    # 而这里衡量的是最终听感模拟效果, 受 --treble-mode 策略影响。
    src_c = src.copy().center().smooth(args.smooth)
    dst_c = dst.copy().center().smooth(args.smooth)
    emulated = src_c.gain + model
    print("\n模拟误差 (源+EQ vs 目标曲线):")
    for lo_f, hi_f, label in ((20, 1000, "低频  20Hz-1k "),
                              (1000, 10000, "中频  1k-10k  "),
                              (10000, 20000, "高频 10k-20k  ")):
        m = (delta.freq >= lo_f) & (delta.freq < hi_f)
        if not m.any():
            continue
        e = emulated[m] - dst_c.gain[m]
        print(f"  {label}: RMS {np.sqrt(np.mean(e ** 2)):5.2f} dB   "
              f"max {np.max(np.abs(e)):5.2f} dB")

    # ---- preamp: 防止削波 ----
    # 按"最坏情况"取: 双二阶的响应随采样率变化, 44.1k/48k 都要算; 且要下探到 10Hz,
    # 因为滤波器在 20Hz 以下仍有增益, 重采样和低频素材会碰到它。
    model_all = fc.filters_response(filters, delta.freq, args.fs)
    boost = fc.worst_case_boost(filters, fs_list=(44100.0, 48000.0))
    preamp = -boost if boost > 0 else 0.0
    print(f"  Preamp   : {preamp:.2f} dB  (最坏峰值 +{boost:.2f} dB @44.1k/48k, 10Hz-20kHz)")

    # ---- 输出 ----
    stem = f"{os.path.splitext(os.path.basename(args.src))[0]}__to__" \
           f"{os.path.splitext(os.path.basename(args.dst))[0]}"
    stem = stem.replace(" ", "_")

    apo = fc.to_autoeq_txt(filters, preamp, f"{src.name} -> {dst.name}")
    path_apo = os.path.join(args.outdir, f"{stem}_ParametricEQ_{args.bands}band.txt")
    with open(path_apo, "w", encoding="utf-8") as fh:
        fh.write(apo + "\n")

    path_rew = os.path.join(args.outdir, f"{stem}_REW.txt")
    with open(path_rew, "w", encoding="utf-8") as fh:
        fh.write(fc.to_rew(filters) + "\n")

    # 目标曲线 CSV (可用于 autoeq.app 的 custom target 上传)
    path_tgt = os.path.join(args.outdir, f"{stem}_target.csv")
    with open(path_tgt, "w", encoding="utf-8") as fh:
        fh.write("frequency,gain\n")
        for f_, g_ in zip(delta.freq, delta.gain):
            fh.write(f"{f_:.2f},{g_:.2f}\n")

    # 拟合结果 CSV
    path_fit = os.path.join(args.outdir, f"{stem}_fit.csv")
    with open(path_fit, "w", encoding="utf-8") as fh:
        fh.write("frequency,target,model,error\n")
        for f_, t_, m_, e_ in zip(delta.freq, delta.gain, model_all, model_all - delta.gain):
            fh.write(f"{f_:.2f},{t_:.2f},{m_:.2f},{e_:.2f}\n")

    print(f"\n已输出:")
    print(f"  {path_apo}   <- Equalizer APO / Peace 直接导入")
    print(f"  {path_rew}   <- REW 粘贴")
    print(f"  {path_tgt}   <- 目标曲线 (可传 autoeq.app)")
    print(f"  {path_fit}   <- 拟合结果对照")

    # ---- 卷积 ----
    if args.convolution:
        taps = fc.design_fir(delta.gain, delta.freq, args.taps, args.fs, args.phase)
        path_wav = os.path.join(args.outdir, f"{stem}_conv_{args.phase}.wav")
        fc.write_convolution_wav(path_wav, taps, args.fs)
        print(f"  {path_wav}   <- Equalizer APO Convolution")

    # ---- 毁HiFi 格式文本 (粘贴进 eq-web 面板) ----
    if args.paste_text:
        path_paste = os.path.join(args.outdir, f"{stem}_paste_毁hifi.txt")
        with open(path_paste, "w", encoding="utf-8") as fh:
            fh.write(fc.to_paste_text(filters) + "\n")
        print(f"  {path_paste}   <- 粘贴进 eq-web 面板")

    # ---- PipeWire filter-chain 预设安装 ----
    if args.pipewire is not None:
        src_short = os.path.splitext(os.path.basename(args.src))[0]
        dst_short = os.path.splitext(os.path.basename(args.dst))[0]
        display = args.pipewire.strip() or f"{src_short} → {dst_short}"

        slug = sanitize_name(f"{src_short}_to_{dst_short}")
        node_stem = "eq_" + fc.slugify(f"{src_short} to {dst_short}")
        conf = fc.to_pipewire_conf(display, node_stem, preamp, filters)
        issues = fc.eqweb_issues(filters, preamp)

        print(f"\nPipeWire 集成:")
        if issues:
            print("  [注意] 与 eq-web 面板的兼容性:")
            for it in issues:
                print(f"    - {it}")
        try:
            path_conf, action = install_pipewire(
                conf, slug, restart=not args.no_restart)
            print(f"  {action}预设: {path_conf}")
            print(f"  显示名  : {display}")
            print(f"  节点名  : effect_input.{node_stem}")
            if args.no_restart:
                print("  未重启服务 —— 生效请执行: "
                      f"systemctl --user restart {SERVICE_UNIT}")
            else:
                print(f"  已重启 {SERVICE_UNIT}")
                print("  在 eq-web 面板 (http://127.0.0.1:8787) 里即可选用该预设")
        except Exception as e:
            print(f"  [错误] 安装失败: {e}", file=sys.stderr)
            return 1

    # ---- 画图 ----
    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # 三条曲线必须用同一基准 (都取 center+smooth 后的版本),
        # 否则源/目标各自的绝对声压级偏移会让"模拟结果"看起来错位。
        fig, ax = plt.subplots(3, 1, figsize=(11, 11), sharex=True)
        ax[0].semilogx(src_c.freq, src_c.gain,
                       label=f"source (centered): {src.name}", lw=1.1, alpha=.85)
        ax[0].semilogx(dst_c.freq, dst_c.gain,
                       label=f"target: {dst.name}", lw=1.1, alpha=.85)
        ax[0].semilogx(delta.freq, emulated,
                       label=f"emulated = source + {len(filters)}-band EQ",
                       color="r", ls="--", lw=1.3)
        ax[0].set_ylabel("dB (centered)")
        ax[0].legend(fontsize=8)
        ax[0].grid(which="both", alpha=.25)
        ax[0].set_title(f"Headphone emulation: {src.name}  ->  {dst.name}")

        ax[1].semilogx(delta.freq, delta.gain, label="required EQ", color="k", lw=1.3)
        ax[1].semilogx(delta.freq, model_all, label=f"{args.bands}-band fit",
                       color="r", ls="--", lw=1.1)
        ax[1].set_ylabel("dB")
        ax[1].legend(fontsize=8)
        ax[1].grid(which="both", alpha=.25)

        ax[2].semilogx(delta.freq, model_all - delta.gain, color="b", lw=1.0)
        ax[2].set_xlabel("Frequency (Hz)")
        ax[2].set_ylabel("fit error (dB)")
        ax[2].grid(which="both", alpha=.25)
        ax[2].set_xlim(20, 20000)
        fig.tight_layout()
        path_png = os.path.join(args.outdir, f"{stem}_plot.png")
        fig.savefig(path_png, dpi=110)
        print(f"  {path_png}   <- 对比图")

    return 0


if __name__ == "__main__":
    sys.exit(main())