# 耳机听感模拟引擎 (fr_core / hp_emulate)

> 本文是模拟引擎的详细说明。它已并入 `eq-web` 项目，
> 所有命令都在 `~/eq-web` 目录下执行。面板 UI 见 `README.md`。

用耳机 A 模拟耳机 B 的听感: 读入两副耳机的频响曲线 (Frequency Response, FR),
生成把 A "拉"成 B 所需的 EQ。**段数不受限制** (10/20/31/任意), 可导出 Equalizer APO /
Peace / REW 参数 EQ, 或直接导出卷积 FIR。

这是对 [毁HIFI 混音器](https://huihifi.com/personal/mixer) 那类"20 段 EQ 模拟"
的桌面端替代: 同一套数学, 但段数、平滑策略、高频处理、导出格式都可控。

---

## 1. 结论: 有没有现成轮子?

有, 而且很成熟。**不需要从零造轮子**, 但现有工具各有取舍:

| 工具 | 形态 | 能力 | 局限 |
|---|---|---|---|
| [AutoEq](https://github.com/jaakkopasanen/AutoEq) | Python 库 + [autoeq.app](https://autoeq.app) 网页 | 目标曲线拟合, 支持**自定义 target** → 上传 B 的曲线即可模拟 B | CLI 需自建环境; 段数由预设决定 |
| [squig.link / Squiglink Lab](https://github.com/squiglink/lab) | 网页 | 海量测量数据库, 可导出 FR 数据 | 只做测量与绘图, 不做 EQ 拟合 |
| [Equalizer APO](https://sourceforge.net/projects/equalizerapo/) + Peace | Windows 系统级 EQ | 执行 EQ, 支持无限段参数 EQ 与卷积 | 只执行, 不计算 |
| [REW (Room EQ Wizard)](https://www.roomeqwizard.com/) | 桌面 | 测量 + EQ 设计 | 偏房间声学, 耳机流程绕 |

**关键事实: AutoEq 官方明确支持"让一副耳机听起来像另一副"** —— 它支持上传自定义
target 曲线, 把 B 的 FR 当 target 就是模拟 B。这也是
[ASR 论坛上已验证的工作流](https://www.audiosciencereview.com/forum/index.php?threads/headphone-emulation-via-autoeq-squiglink-and-equalizer-apo.62682/):
Squiglink 下载 A、B 的 FR → AutoEq 上传 A 作为测量、B 作为自定义 target →
导出 Equalizer APO 配置。

### 那为什么还自己写?

因为在这台机器上 **AutoEq 的 Python 包装不上**: `autoeq 4.1.2` 声明
`requires_python = ">=3.8,<3.12"`, 而系统是 Python 3.12.3。pip 于是回退到旧版本,
旧版本的 numpy 依赖在 3.12 上编译失败 (`pkgutil.ImpImporter` 已被移除):

```
ERROR: Failed to build 'numpy' when getting requirements to build wheel
```

所以在 Python 3.12 环境下, 要么用 `autoeq.app` 网页版, 要么自己实现。
本项目用 **numpy + scipy** 重写了所需部分 (即 `fr_core.py`), 在 3.12 上直接可用, 并且把段数放开到任意。

---

## 2. 实测效果

以两组真实测量为例 (均来自 oratory1990, 同一套人工头):

| 指标 | HD650 → HD600 (10段, 差异小) | HD650 → K371 (20段, 差异大) |
|---|---|---|
| 拟合误差 RMS (20Hz–10kHz) | 0.04 dB | 0.14 dB |
| **模拟误差** 20Hz–1kHz | **0.04 dB** | 0.14 dB |
| **模拟误差** 1k–10kHz | **0.06 dB** | 0.25 dB |
| **模拟误差** 10k–20kHz | 0.75 dB | 3.00 dB |
| 最大单滤波器增益 | 1.82 dB | 11.05 dB (低频搁架) |
| 自动 Preamp | −2.51 dB | −12.04 dB |

注意区分两个指标:
- **拟合误差** = 参数 EQ 逼近"所需曲线"的程度;
- **模拟误差** = `源耳机 + EQ` 与目标耳机的实际差距 ← **这才是"模得像不像"**。

段数验证 (HD650→HD600): 10 段 0.04 dB / 20 段 0.08 dB / 31 段 0.08 dB / 40 段 0.05 dB。
**段数并非越多越准**: 差异小时 10 段已到极限, 差异大或曲线复杂时更多段才有意义。

高频误差明显更大, 这是物理限制而非 bug (见 §5)。

### 自检: 模拟自己应当输出全 0

`HD 650 → HD 650` 的结果是 **0.00 dB 误差、所有滤波器增益 0.00 dB** —— 说明流程不会
凭空捏造 EQ。这是最有价值的一条回归测试 (它同时也验证了减法方向没有写反)。

### 第二个例子: 大差异场景

`HD 650 → AKG K371` (开放动圈 → 封闭) 差异大得多: 低频需要 +12.6dB 提升
(20Hz 处 HD650=-6.9dB, K371=+5.7dB), 因此 Preamp 会被自动压到 -12dB 以防止削波。
这**不是 bug**, 而是"用低频偏少的耳机模拟低频偏多的耳机"的必然代价: 整体音量必须
降下来换取低频余量。该场景 20 段拟合 RMS ≈ 0.14 dB。

> 注: 不同测量源的曲线**不可以混用**。上面两副耳机都来自 oratory1990 同一套人工头。

---

## 3. 用法

```bash
# 基础: HD650 → HD600, 10 段参数 EQ
python3 hp_emulate.py \
  --from "measurements/Sennheiser HD 650.csv" \
  --to   "measurements/Sennheiser HD 600.csv" \
  --bands 10 --outdir out --plot

# 桌面端暴力模式: 31 段 + 卷积 FIR + 低频 +6dB
python3 hp_emulate.py --from A.csv --to B.csv \
  --bands 31 --bass-boost 6 --convolution --plot

# 校验导出的 FIR 是否真的还原了目标曲线
python3 verify_fir.py out/*_conv_minimum.wav out/*_target.csv
```

依赖: `numpy`, `scipy` (绘图需 `matplotlib`)。

### 主要参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bands` | 10 | EQ 段数, **任意整数**, 桌面端可上 31+ |
| `--smooth` | 1/6 | 平滑倍频程。越大越保守, 越小越"贴"测量噪声 |
| `--treble-mode` | shelf | 高频策略: `match`(硬拟合) / `shelf`(整体搁架) / `ignore`(不动高频) |
| `--treble-fc` | 8000 | 高频搁架过渡起点 |
| `--max-gain` | 15 | 单个滤波器增益上限, 防止破坏性尖峰 |
| `--reg` | 0.05 | 增益正则化, 抑制"互相抵消的大增益滤波器" |
| `--bass-boost` | 0 | 叠加低频搁架 (哈曼式) |
| `--tilt` | 0 | 整体倾斜 dB/octave (负数=变暗) |
| `--convolution` | off | 额外导出 32-bit float WAV 卷积滤波器 |

### 输出文件

- `*_ParametricEQ_*.txt` — Equalizer APO / Peace 直接导入
- `*_REW.txt` — 粘贴进 REW
- `*_target.csv` — 所需 EQ 曲线 (也可上传到 autoeq.app 当 custom target)
- `*_fit.csv` — 目标 / 拟合 / 误差 逐点对照, 便于自查
- `*_conv_minimum.wav` — 卷积 FIR (32-bit float, Equalizer APO Convolution 可直接加载)
- `*_plot.png` — 三联图: 源 vs 目标 vs 模拟结果 / 所需 EQ vs 拟合 / 拟合误差

---

## 4. 怎么拿到频响数据

1. **最省事**: 直接用本仓库的 `measurements/*.csv` (来自 AutoEq 收集的
   [oratory1990](https://www.reddit.com/r/oratory1990/wiki/index/list_of_presets/) /
   crinacle 等测量)。
2. 从 [squig.link](https://squig.link) 选耳机 → 导出 FR 文本。
3. 其它 AutoEq 测量:
   `https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master/measurements/<source>/data/<类目>/<型号>.csv`

```bash
curl -o "Sennheiser HD 650.csv" \
 "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master/measurements/oratory1990/data/over-ear/Sennheiser%20HD%20650.csv"
```

**重要**: 源和目标的 FR 必须来自**同一测量源/同一套人工头**, 否则差异里混入了
测量设备差异, 模拟结果不可信。

---

## 5. 为什么不可能"完美模拟" (诚实说明)

1. **只看频响。** 耳机的时域特性 (群延迟/共振)、失真、声场/成像、开放 vs 封闭的
   泄露特性, EQ 都改不了。
2. **10kHz 以上不可信。** 耳道共振因人而异, 同一个耳机不同人测出的高频能差 10dB。
   所以本项目默认在这里改用整体搁架, 而不是硬拟合——硬拟合等于把测量噪声当信号。
3. **削波风险。** 大增益提升必须配负 Preamp。本项目自动计算 Preamp, 但用户若手动
   改动滤波器, 需自行留意。
4. **需要耳机本身能承受 EQ。** 低失真的耳机 (如 HD650/HD600) 效果好; 大增益提升
   低频会显著增加失真。

---

## 6. 实现要点 / 踩过的坑

代码里注释标记了关键决策, 这里列最容易出错的几条:

- **减法方向 (最致命).** "把 A 变成 B 所需 EQ" = `B - A`。方向写反会得到完全相反的
  EQ, 而且**拟合误差看起来依然完美** (它忠实地拟合了错的目标)。本项目用恒等式
  `src + (dst - src) == dst` 做单元自检。
- **居中 (center).** 两条曲线的绝对声压级没有可比性, 必须先各自归零再相减。
  绘图时务必用同一基准, 否则模拟结果会看起来整体偏移。
- **平滑.** 未平滑的测量曲线含有大量窄带噪声毛刺, 直接拟合会得出又尖又多的滤波器。
  1/6 倍频程是常用默认值。
- **高频硬切换会产生阶跃.** 搁架过渡必须平滑 (`smoothstep`), 否则 FIR 会振铃/预回声。
- **FIR 抽头数必须是奇数.** `firwin2` 偶数抽头是 Type II 滤波器, 强制 Nyquist 处增益为 0,
  直接抛 `ValueError`。
- **互相抵消 / 同频堆叠的滤波器.** 最小二乘会解出两类难看的结果:
  1. 中心频率相近、**增益相反**的滤波器 (如 +14.2dB / -12.2dB), 净效果接近 0 却白占段数,
     手动微调就会崩 —— 这是"脆弱解";
  2. **同号同频堆叠**的滤波器 (如 3 个都顶在 20Hz 边界, 各 +3dB), 等价于一个 +9dB
     滤波器却占了 3 段。
  对策分三层: `prune_and_refine()` 移除低贡献段; `merge_coincident()` 合并同频同号滤波器
  (异号不合并, 因为相加无意义); 重拟合之后再合并一次 (重拟合会再次产生堆叠)。
- **正则化要用 L1 而不是 L2.** 惩罚 `gain²` 会**鼓励拆分**: 把一个大增益拆成同频的多个
  小增益能降低 `g1²+g2² < (g1+g2)²`。改用 L1 (惩罚 `|gain|`, 代码里以
  `sqrt(reg·|g|)` 的残差形式实现) 对同号叠加是中性的, 既压住了大增益, 又不会诱使拆分。
- **感知加权.** 在对数频率域等权拟合, 并对 >10kHz 降权, 比线性频率域合理得多。
- **多起点重启.** 拟合是非凸问题, 单次最小二乘容易陷入局部最优。

---

## 7. PipeWire / eq-web 集成 (Ubuntu)

本工具可以直接把生成的 EQ 装成 PipeWire 的 filter-chain 预设, 并被
[eq-web](https://github.com/MAK1MA-AG0NY/eq-web) 面板自动识别。

```bash
# 生成 + 安装为滤波器预设 + 重启 filter-chain.service
python3 hp_emulate.py \
  --from "measurements/Sennheiser HD 650.csv" \
  --to   "measurements/Sennheiser HD 600.csv" \
  --bands 20 --pipewire "HD650 → HD600 模拟"
```

输出:

```
新建预设: ~/.config/pipewire/filter-chain.conf.d/66-Sennheiser-HD-650_to_Sennheiser-HD-600.conf
显示名  : HD650 → HD600 模拟
节点名  : effect_input.eq_sennheiser-hd-650-to-sennheiser-hd-600
已重启 filter-chain.service
```

之后在 `http://127.0.0.1:8787` 面板里就能像其它预设一样选用。

### 相关参数

| 参数 | 说明 |
|---|---|
| `--pipewire [显示名]` | 安装为 filter-chain 预设, 写入 `~/.config/pipewire/filter-chain.conf.d/` 并重启服务 |
| `--no-restart` | 只写配置不重启 (自己决定何时生效) |
| `--paste-text` | 额外输出毁HiFi 格式文本, 可直接粘贴进 eq-web 面板的解析框 |

**重复运行会原地更新同一个预设** (按 headphone 名生成的 slug 匹配), 方便反复调参。

### 兼容性说明

- 生成的 conf 与 eq-web 的 `_render_conf()` 格式完全一致, 面板靠
  `node.description` / `effect_input.*` 识别虚拟声卡, 因此能被自动发现和管理。
- **段数 / Q 的上限是面板的校验, 不是 PipeWire 的限制。** 实测 filter-chain
  可正常加载 **256 段、Q=100**。已把面板 (server.py) 的边界放宽为
  **128 段 / Q 0.1~50**, 与本工具的 `EQWEB_LIMITS` 保持一致。
  唯一真正的约束是性能: 面板的 `suggest_preamp()` 耗时随段数线性增长
  (32 段≈1.0s, 64 段≈2.1s, 128 段≈4.1s), 段数过大会让面板卡。
- 实际使用上**不必追求多段**: 前面的实测里 10 段就已达 0.04 dB。
  段数多只在"两副耳机差异很大且曲线复杂"时才有意义 (如 HD650→K371 用 20 段)。
- **Preamp 计算比面板更准**: 面板对含搁架的配置采用"正增益求和"的保守上界
  (会放大衰减量); 本工具直接计算级联滤波器的真实峰值, 且同时覆盖 44.1k/48k
  与 10Hz–20kHz。两者都不会削波, 本工具的衰减量更小、留更多余量。

### 已验证的正确性

- `bq_lowshelf` / `bq_highshelf` 经隔离 PipeWire 实例验证可用 (负对照通过)。
- 本工具的 `Biquad` 与 PipeWire `spa/plugins/audioconvert/biquad.c` 的公式
  **逐项一致**, 频响最大差异 **2×10⁻¹¹ dB**(浮点精度内), 因此实际听感与预期曲线一致。

### 7.1 面板 UI（推荐方式）

`eq-web` 面板已内置"耳机模拟"，不需要命令行：

1. 打开 <http://127.0.0.1:8787>
2. 左侧 **EQ 预设** 卡片点 **🎧 模拟耳机**
3. **先选测量源**（两副耳机只能同源 —— 从结构上杜绝跨源混用）
4. 在左右两个列表里分别选「我的耳机」和「目标耳机」（各带搜索框）
5. 选段数与高频策略，点 **开始模拟**（约 5~15 秒，后台任务不卡面板）
6. 看低频/中频/高频的模拟误差，满意就点 **创建预设**

生成后就像其它预设一样出现在列表里，可继续在编辑器里微调。

**「找同源」辅助**：不确定用哪个源时，展开模态框里的
「不知道用哪个源？点这里『找同源』」，输入任意耳机型号，会列出所有收录它的源，
可一键切换过去。两副耳机都出现在同一个源里才能安全互模拟。

**高频策略怎么选**（真实取舍，不是 bug）：

| 策略 | 高频精度 | 预增益代价 | 适用 |
|---|---|---|---|
| 保守 `shelf` | 较差 | 小 | 默认；不信任 10kHz 以上测量时 |
| 完全跟目标 `match` | 最好 | **大**（可能多付 6~7dB） | 想尽量还原目标耳机音色时 |
| 不修正 `ignore` | 最差 | 中 | 只关心低频/中频时 |

实测 HE400se → HE1000 Unveiled（10 段）：
`shelf` 高频误差 5.66dB / 预增益 −7.09dB；`match` 高频误差 **0.79dB** / 预增益 **−13.62dB**。

> `match` 会在 10kHz 以上产生较大增益的滤波器（有时顶到 ±15dB 上限），
> 因为它在追测量噪声。追求稳健就用 `shelf`。

### 7.2 命令行方式

```bash
python3 hp_emulate.py --from A.csv --to B.csv --bands 20 --pipewire "我的模拟"
```

### 部署注意：服务解释器必须有 numpy/scipy

`eq-web` 的 systemd 服务**不能**用 `/usr/bin/python3` —— Ubuntu 的系统 Python
默认没有 numpy/scipy，模拟功能会报"缺少 numpy/scipy"。本机已改用项目自带 venv：

```ini
ExecStart=/home/mak1ma/eq-web/.venv/bin/python3 /home/mak1ma/eq-web/server.py
```

venv 重建方式：

```bash
cd ~/eq-web && /usr/bin/python3 -m venv .venv && .venv/bin/pip install numpy scipy
systemctl --user daemon-reload && systemctl --user restart eq-web.service
```

面板的**索引/搜索/同源检查**是纯标准库实现，缺 numpy 也能用；
只有真正做 DSP 的 `/api/sim/run` 需要 numpy/scipy。

### 本机安装记录

`66-HIFIMAN-HE400se-→-HIFIMAN-HE1000-Unveiled.conf` (10 段, preamp −13.62 dB) 已安装，
并被 eq-web 面板识别 (`loaded=True`)。改动前的原配置备份在
`/tmp/filter-chain-backup-*.tgz`。

---

## 8. 文件说明

| 文件 | 说明 |
|---|---|
| `fr_core.py` | 核心库: FR 加载/插值/平滑、RBJ 双二阶滤波器、参数 EQ 拟合、FIR 设计、各格式导出 |
| `hp_emulate.py` | 命令行主程序 |
| `verify_fir.py` | 卷积 FIR 校验 (手动解析 32-bit float WAV, 对比目标曲线, 检查最小相位) |
| `measurements/` | 频响数据 (AutoEq 收集) |
| `out/` | 输出结果 |
