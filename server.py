#!/usr/bin/python3
"""eq-web: local web control panel for the HE400se PARA II PipeWire EQ.

Stdlib only. Binds 127.0.0.1:8787. All subprocess calls use arg lists
with timeout=5.
"""
import json
import math
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
PORT = 8787

HOME = Path(os.path.expanduser("~"))
EQ_DIR = HOME / ".config/pipewire/filter-chain.conf.d"
EQ_CONF = EQ_DIR / "60-he400se-para2-eq.conf"
STATE_DIR = HOME / ".local/state/eq-web"
STATE_FILE = STATE_DIR / "state.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"

EQ_NODE_NAME = "effect_input.he400se_para2"
EQ_DESCRIPTION = "HE400se → PARA II EQ"
EQ_INPUT_PREFIX = "effect_input."
EQ_OUTPUT_PREFIX = "effect_output."
SERVICE_UNIT = "filter-chain.service"

MAX_BODY = 64 * 1024
MIN_BANDS = 1
MAX_BANDS = 32

# Serialize request handling: concurrent pw-dump/pw-link/wpctl invocations
# can race and produce corrupted output. Single-user panel, so serialization
# is correct.
_REQ_LOCK = threading.Lock()

TYPE_TO_LABEL = {
    "peaking": "bq_peaking",
    "lowshelf": "bq_lowshelf",
    "highshelf": "bq_highshelf",
}
LABEL_TO_TYPE = {v: k for k, v in TYPE_TO_LABEL.items()}


class ApiError(Exception):
    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def run(args, timeout=5):
    """Run a subprocess; returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ApiError(f"找不到命令: {args[0]}")
    except subprocess.TimeoutExpired:
        raise ApiError(f"命令超时: {' '.join(args)}")
    return p.returncode, p.stdout, p.stderr


def must_run(args, timeout=5):
    rc, out, err = run(args, timeout)
    if rc != 0:
        raise ApiError(f"命令失败 ({args[0]}): {err.strip() or out.strip()}")
    return out


# ---------------------------------------------------------------- sinks

def pw_dump():
    rc, out, err = run(["pw-dump"])
    if rc != 0:
        raise ApiError(f"pw-dump 失败: {err.strip() or out.strip()}")
    last = None
    for attempt in range(3):
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            last = e
            if attempt < 2:
                time.sleep(0.2)
                rc, out, err = run(["pw-dump"])
                if rc != 0:
                    raise ApiError(f"pw-dump 失败: {err.strip() or out.strip()}")
    raise ApiError(f"pw-dump 输出解析失败: {last} (已重试 3 次)")


def _sinks_from_dump(data):
    sinks = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name") or ""
        if not name:
            continue
        sinks.append({
            "id": int(obj["id"]),
            "name": name,
            "description": props.get("node.description") or name,
        })
    return sinks


def list_sinks():
    return _sinks_from_dump(pw_dump())


def is_eq_name(name):
    """Any filter-chain virtual sink (multiple presets exist)."""
    return bool(name) and name.startswith(EQ_INPUT_PREFIX)


def eq_sinks(sinks):
    return [s for s in sinks if is_eq_name(s["name"])]


def physical_sinks(sinks):
    """Non-virtual Audio/Sink nodes, alsa_output.* preferred."""
    cands = [s for s in sinks if not is_eq_name(s["name"])]
    cands.sort(key=lambda s: 0 if s["name"].startswith("alsa_output") else 1)
    return cands


def parse_pw_links():
    """pw-link -l -> directed (src_port, dst_port) pairs, e.g.
    ("effect_output.x:output_FL", "alsa_output.y:playback_FL")."""
    rc, out, _ = run(["pw-link", "-l"])
    if rc != 0:
        return []
    links = []
    cur = None
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("|->") or s.startswith("|<-"):
            if cur:
                peer = s[3:].strip()
                links.append((cur, peer) if s.startswith("|->") else (peer, cur))
        else:
            cur = s
    return links


def port_node(port):
    return port.rsplit(":", 1)[0] if ":" in port else port


def node_port_names(data, node_id, prefix):
    """Sorted port names of a node whose names start with prefix."""
    names = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Port":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if str(props.get("node.id")) != str(node_id):
            continue
        pn = props.get("port.name") or ""
        if pn.startswith(prefix):
            names.append(pn)
    return sorted(names)


def _pair_ports(out_ports, in_ports):
    """Match output_FL->playback_FL style by channel suffix; fall back to zip."""
    o = {p.rsplit("_", 1)[-1]: p for p in out_ports}
    t = {p.rsplit("_", 1)[-1]: p for p in in_ports}
    pairs = [(o[suf], t[suf]) for suf in o if suf in t]
    if not pairs and out_ports and in_ports:
        pairs = list(zip(out_ports, in_ports))
    return pairs


def downstream_of(default_name, links, sinks):
    """Physical sink fed by the default EQ's effect_output stream, if any."""
    if not is_eq_name(default_name):
        return None
    out_node = EQ_OUTPUT_PREFIX + default_name[len(EQ_INPUT_PREFIX):]
    dst_nodes = {port_node(d) for s, d in links if port_node(s) == out_node}
    phys = {s["name"]: s for s in physical_sinks(sinks)}
    for n in dst_nodes:
        if n in phys:
            return phys[n]
    return None


def default_node_name():
    """Source of truth for the default sink: wpctl inspect."""
    out = must_run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"])
    m = re.search(r'node\.name\s*=\s*"?([^"\n]+)"?', out)
    d = re.search(r'node\.description\s*=\s*"?([^"\n]+)"?', out)
    if not m:
        raise ApiError("无法解析默认输出设备 (wpctl inspect)")
    return m.group(1).strip(), (d.group(1).strip() if d else None)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def remember_non_eq_default(sinks, default_name):
    """Persist the last non-EQ default sink id for toggle-off."""
    if default_name is None:
        return
    for s in sinks:
        if s["name"] == default_name and not is_eq_name(s["name"]):
            state = load_state()
            if state.get("last_non_eq_default", {}).get("id") != s["id"]:
                state["last_non_eq_default"] = {"id": s["id"], "name": s["name"]}
                save_state(state)
            return


def set_default_sink(sink_id):
    must_run(["wpctl", "set-default", str(int(sink_id))])


# ---------------------------------------------------------------- volume chain

def wp_volume(node):
    """(value, muted) for one node id or special target."""
    out = must_run(["wpctl", "get-volume", str(node)])
    m = re.search(r"Volume:\s*([0-9.]+)", out)
    if not m:
        raise ApiError(f"无法解析音量 (wpctl get-volume {node})")
    return float(m.group(1)), "MUTED" in out.upper()


def get_volume(sinks=None):
    """Volume snapshot for the active chain.

    Default is a virtual EQ: real loudness = vol(EQ) x vol(downstream physical),
    both independently controllable. Default is physical: just its own volume.
    Muted = any chain member muted. Never writes anything.
    """
    if sinks is None:
        sinks = list_sinks()
    default_name, _ = default_node_name()
    via_eq = is_eq_name(default_name)
    eq_info = None
    physical = None
    if via_eq:
        eq_sink = next((s for s in sinks if s["name"] == default_name), None)
        if eq_sink is None:
            raise ApiError("无法解析默认输出设备")
        v, mu = wp_volume(eq_sink["id"])
        eq_info = {
            "id": eq_sink["id"],
            "name": eq_sink["name"],
            "description": eq_sink["description"],
            "value": round(v, 4),
            "muted": mu,
        }
        physical = downstream_of(default_name, parse_pw_links(), sinks)
        if physical is None:
            phys = physical_sinks(sinks)
            physical = phys[0] if phys else None
    else:
        physical = next((s for s in sinks if s["name"] == default_name), None)
    if eq_info is None and physical is None:
        raise ApiError("无法解析默认输出设备")
    phys_info = None
    value = 1.0
    muted = False
    if physical is not None:
        v, mu = wp_volume(physical["id"])
        phys_info = {
            "id": physical["id"],
            "name": physical["name"],
            "description": physical["description"],
            "value": round(v, 4),
            "muted": mu,
        }
        if eq_info is not None:
            value = eq_info["value"] * v
            muted = eq_info["muted"] or mu
        else:
            value = v
            muted = mu
    return {
        "value": round(value, 4),
        "muted": muted,
        "via_eq": via_eq,
        "output": (
            {"id": phys_info["id"], "name": phys_info["name"],
             "description": phys_info["description"]}
            if phys_info else None
        ),
        "eq": eq_info,
        "physical": phys_info,
    }


def remember_preset_volume(sink):
    """Store the current device volume of an EQ preset, keyed by node.name."""
    v, _ = wp_volume(sink["id"])
    state = load_state()
    mem = state.setdefault("preset_volumes", {})
    if mem.get(sink["name"]) != round(v, 4):
        mem[sink["name"]] = round(v, 4)
        save_state(state)


def apply_preset_volume(sink):
    """Restore a preset's remembered device volume, if an entry exists."""
    v = (load_state().get("preset_volumes") or {}).get(sink["name"])
    if isinstance(v, (int, float)) and 0.0 < v <= 1.5:
        run(["wpctl", "set-volume", str(sink["id"]), f"{v:.4f}"])


def forget_preset_volume(node_name):
    """Drop a preset's memory entry (on preset deletion)."""
    if not node_name:
        return
    state = load_state()
    mem = state.get("preset_volumes")
    if isinstance(mem, dict) and node_name in mem:
        mem.pop(node_name)
        save_state(state)


def physical_for_mode(default_name, sinks):
    """The physical device whose volume belongs to the mode keyed by
    default_name: the downstream of an EQ default, the default itself in
    direct mode, else the first physical sink."""
    if is_eq_name(default_name):
        physical = downstream_of(default_name, parse_pw_links(), sinks)
        if physical is None:
            phys = physical_sinks(sinks)
            physical = phys[0] if phys else None
    else:
        physical = next((s for s in sinks if s["name"] == default_name), None)
    return physical


def capture_family_memory(sinks):
    """Remember the current physical volume under the current default's
    node.name (mode key) before a switch."""
    try:
        default_name, _ = default_node_name()
    except ApiError:
        return
    if not default_name:
        return
    physical = physical_for_mode(default_name, sinks)
    if physical is None:
        return
    v, _ = wp_volume(physical["id"])
    state = load_state()
    mem = state.setdefault("family_volumes", {})
    if mem.get(default_name) != round(v, 4):
        mem[default_name] = round(v, 4)
        save_state(state)


def apply_family_memory(default_name, sinks):
    """Restore a mode's remembered Family value onto the physical device."""
    v = (load_state().get("family_volumes") or {}).get(default_name)
    if isinstance(v, (int, float)) and 0.0 < v <= 1.5:
        physical = physical_for_mode(default_name, sinks)
        if physical is not None:
            run(["wpctl", "set-volume", str(physical["id"]), f"{v:.4f}"])


def forget_family_volume(node_name):
    """Drop a mode's Family memory entry (on preset deletion)."""
    if not node_name:
        return
    state = load_state()
    mem = state.get("family_volumes")
    if isinstance(mem, dict) and node_name in mem:
        mem.pop(node_name)
        save_state(state)


def capture_eq_memory(sinks):
    """If the current default is an EQ, remember its live device volume
    (captures panel and external e.g. GNOME changes) before a switch."""
    try:
        default_name, _ = default_node_name()
    except ApiError:
        return
    if not is_eq_name(default_name):
        return
    cur = next((s for s in sinks if s["name"] == default_name), None)
    if cur is not None:
        remember_preset_volume(cur)


def after_default_switch(target):
    """Post-switch housekeeping: restore the target mode's remembered volumes
    (EQ device + Family) and repair links."""
    time.sleep(0.3)  # let WirePlumber start re-routing
    sinks = list_sinks()
    if is_eq_name(target["name"]):
        fresh = next((s for s in sinks if s["name"] == target["name"]), None)
        if fresh is not None:
            apply_preset_volume(fresh)
    apply_family_memory(target["name"], sinks)
    repair_links()


# ---------------------------------------------------------------- link repair

def repair_links():
    """Fix routing: no virtual->virtual EQ links; every effect_output stream
    must reach a physical sink. Two rounds (WirePlumber re-routes async).
    """
    fixed = {"disconnected": 0, "reconnected": 0}
    clean = False
    for _ in range(2):
        links = parse_pw_links()
        for src, dst in links:
            if (port_node(src).startswith(EQ_OUTPUT_PREFIX)
                    and port_node(dst).startswith(EQ_INPUT_PREFIX)):
                rc, _, _ = run(["pw-link", "-d", src, dst])
                if rc == 0:
                    fixed["disconnected"] += 1
        if fixed["disconnected"]:
            time.sleep(0.4)
            links = parse_pw_links()

        data = pw_dump()
        sinks = _sinks_from_dump(data)
        out_nodes = {}
        for obj in data:
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = (obj.get("info") or {}).get("props") or {}
            name = props.get("node.name") or ""
            if name.startswith(EQ_OUTPUT_PREFIX):
                out_nodes[name] = obj["id"]
        linked = {port_node(s) for s, _ in links
                  if port_node(s).startswith(EQ_OUTPUT_PREFIX)}
        orphans = sorted(n for n in out_nodes if n not in linked)

        if orphans:
            try:
                dname, _ = default_node_name()
            except ApiError:
                dname = None
            target = downstream_of(dname, links, sinks) if dname else None
            if target is None:
                phys = physical_sinks(sinks)
                target = phys[0] if phys else None
            if target is None:
                break
            tports = node_port_names(data, target["id"], "playback")
            for name in orphans:
                oports = node_port_names(data, out_nodes[name], "output")
                for op, tp in _pair_ports(oports, tports):
                    rc, _, _ = run(
                        ["pw-link", f"{name}:{op}", f"{target['name']}:{tp}"])
                    if rc == 0:
                        fixed["reconnected"] += 1
            time.sleep(0.4)
            links = parse_pw_links()

        linked = {port_node(s) for s, _ in links
                  if port_node(s).startswith(EQ_OUTPUT_PREFIX)}
        bad_v2v = any(
            port_node(d).startswith(EQ_INPUT_PREFIX)
            for s, d in links if port_node(s).startswith(EQ_OUTPUT_PREFIX))
        clean = (not bad_v2v) and out_nodes.keys() <= linked
        if clean:
            break
    return {"ok": True, "fixed": fixed, "links_clean": clean}


# ---------------------------------------------------------------- status

def service_status():
    rc, out, _ = run(["systemctl", "--user", "is-active", SERVICE_UNIT])
    s = out.strip()
    return s if s else "unknown"


def status_payload():
    sinks = list_sinks()
    default_name, default_desc = default_node_name()
    eq_device = None
    for s in sinks:
        if s["name"] == EQ_NODE_NAME:
            eq_device = {"id": s["id"], "name": s["name"], "description": s["description"]}
    default_sink = None
    for s in sinks:
        if s["name"] == default_name:
            default_sink = {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "is_eq": is_eq_name(s["name"]),
            }
    if default_sink is None:
        # fallback: build a minimal record from wpctl inspect itself
        default_sink = {
            "id": None,
            "name": default_name or "",
            "description": default_desc or default_name or "",
            "is_eq": is_eq_name(default_name or ""),
        }
    remember_non_eq_default(sinks, default_name)
    sink_list = [
        {
            "id": s["id"],
            "name": s["name"],
            "description": s["description"],
            "is_default": s["name"] == default_name,
            "is_eq": is_eq_name(s["name"]),
        }
        for s in sinks
    ]
    return {
        "eq_service": service_status(),
        "eq_device": eq_device,
        "default_sink": default_sink,
        "sinks": sink_list,
        "volume": get_volume(sinks),
    }


# ---------------------------------------------------------------- eq conf

def _num(text, key):
    m = re.search(rf'"{key}"\s*=\s*(-?\d+(?:\.\d+)?)', text)
    if not m:
        raise ApiError(f'配置解析失败: 未找到 "{key}"')
    return float(m.group(1))


def _band_type(line):
    """Return (type, label) if the line is a biquad band control line."""
    if "control" not in line:
        return None
    for label, btype in LABEL_TO_TYPE.items():
        if label in line:
            return btype
    return None


def read_preset(path):
    """Parse one preset conf file into a payload dict."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ApiError(f"读取 EQ 配置失败: {e}")
    m = re.search(r'node\.description\s*=\s*"([^"]*)"', text)
    description = m.group(1) if m else Path(path).stem
    m = re.search(r'node\.name\s*=\s*"?([\w.\-]+)"?', text)
    node_name = m.group(1) if m else ""
    bands = []
    preamp_db = None
    for line in text.splitlines():
        btype = _band_type(line)
        if btype:
            bands.append({
                "freq": _num(line, "Freq"),
                "q": _num(line, "Q"),
                "gain": _num(line, "Gain"),
                "type": btype,
            })
        elif "linear" in line and "Mult" in line and "control" in line:
            mult = _num(line, "Mult")
            if mult <= 0:
                raise ApiError("Mult 值非法")
            preamp_db = round(20.0 * math.log10(mult), 2)
    if preamp_db is None:
        raise ApiError("配置解析失败: 未找到 pre (linear/Mult) 节点")
    if not bands:
        raise ApiError("配置解析失败: 未找到任何滤波波段")
    return {
        "file": Path(path).name,
        "description": description,
        "node_name": node_name,
        "preamp_db": preamp_db,
        "bands": bands,
    }


def preset_path(file):
    """Validate a preset filename strictly; returns the resolved path."""
    if not isinstance(file, str) or not file:
        raise ApiError("缺少 file 字段", 400)
    if "/" in file or "\\" in file or ".." in file or not file.endswith(".conf"):
        raise ApiError("非法文件名", 400)
    p = (EQ_DIR / file).resolve()
    if p.parent != EQ_DIR:
        raise ApiError("非法文件名", 400)
    if not p.is_file():
        raise ApiError("预设不存在", 404)
    return p


def list_preset_files():
    try:
        return sorted(EQ_DIR.glob("*.conf"))
    except OSError as e:
        raise ApiError(f"扫描预设目录失败: {e}")


def list_presets():
    files = list_preset_files()
    sinks = list_sinks()
    sink_by_name = {s["name"]: s for s in sinks}
    try:
        default_name, _ = default_node_name()
    except ApiError:
        default_name = None
    presets = []
    for f in files:
        try:
            info = read_preset(f)
        except ApiError:
            continue  # skip unreadable/malformed preset files
        sink = sink_by_name.get(info["node_name"])
        presets.append({
            "file": info["file"],
            "description": info["description"],
            "bands": len(info["bands"]),
            "node_name": info["node_name"],
            "sink": (
                {"id": sink["id"], "loaded": True, "is_default": sink["name"] == default_name}
                if sink else None
            ),
        })
    return {"presets": presets}


def _validate_eq(payload):
    if not isinstance(payload, dict):
        raise ApiError("请求体必须是 JSON 对象", 400)
    try:
        preamp = float(payload["preamp_db"])
    except (KeyError, TypeError, ValueError):
        raise ApiError("preamp_db 必须是数字", 400)
    if not (-24.0 <= preamp <= 0.0):
        raise ApiError("preamp_db 超出范围 (-24 ~ 0)", 400)
    bands = payload.get("bands")
    if not isinstance(bands, list) or not (MIN_BANDS <= len(bands) <= MAX_BANDS):
        raise ApiError(f"bands 必须是包含 {MIN_BANDS}~{MAX_BANDS} 个波段的数组", 400)
    out = []
    for i, b in enumerate(bands, 1):
        try:
            freq = float(b["freq"])
            q = float(b["q"])
            gain = float(b["gain"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(f"第 {i} 段参数缺失或非法", 400)
        if not (10.0 <= freq <= 22000.0):
            raise ApiError(f"第 {i} 段频率超出范围 (10 ~ 22000 Hz)", 400)
        if not (0.1 <= q <= 20.0):
            raise ApiError(f"第 {i} 段 Q 超出范围 (0.1 ~ 20)", 400)
        if not (-24.0 <= gain <= 24.0):
            raise ApiError(f"第 {i} 段增益超出范围 (-24 ~ +24 dB)", 400)
        btype = b.get("type", "peaking")
        if btype not in TYPE_TO_LABEL:
            raise ApiError(f"第 {i} 段滤波类型非法: {btype}", 400)
        out.append({"freq": freq, "q": q, "gain": gain, "type": btype})
    return preamp, out


def _replace_token(line, key, fmt, align_left=False):
    """Replace the numeric token after `key` =, keeping column width."""
    pat = re.compile(rf'("{key}"\s*=\s*)(-?\d+(?:\.\d+)?)')
    m = pat.search(line)
    if not m:
        raise ApiError(f'配置重写失败: 未找到 "{key}"')
    new = fmt
    old = m.group(2)
    if len(new) < len(old):
        pad = " " * (len(old) - len(new))
        new = (new + pad) if align_left else (pad + new)
    elif len(new) > len(old):
        pass  # value grows; following whitespace still keeps line readable
    return line[:m.start()] + m.group(1) + new + line[m.end():]


def write_eq(path, preamp, bands):
    """Rewrite band values (and labels) in an existing preset file, in place."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ApiError(f"读取 EQ 配置失败: {e}")
    lines = text.splitlines(keepends=True)
    idx = 0
    mult = round(10.0 ** (preamp / 20.0), 4)
    replaced_pre = False
    pat_label = re.compile(r"label\s*=\s*\S+")
    for i, line in enumerate(lines):
        btype = _band_type(line)
        if btype and idx < len(bands):
            b = bands[idx]
            idx += 1
            new_label = TYPE_TO_LABEL[b["type"]]
            if btype != new_label:
                line = pat_label.sub(f"label = {new_label}", line, count=1)
            line = _replace_token(line, "Freq", f"{b['freq']:.1f}", align_left=True)
            line = _replace_token(line, "Q", f"{b['q']:.3f}")
            line = _replace_token(line, "Gain", f"{b['gain']:.3f}")
            lines[i] = line
        elif "linear" in line and "Mult" in line and "control" in line:
            line = _replace_token(line, "Mult", f"{mult:.4f}")
            lines[i] = line
            replaced_pre = True
    if idx != len(bands) or not replaced_pre:
        raise ApiError(f"配置重写失败: 波段匹配数 {idx}, pre 替换 {replaced_pre}")
    # atomic write
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- text parser

TYPE_MAP_CN = {"峰值": "peaking", "低架": "lowshelf", "高架": "highshelf"}


def _parse_filter_line(line, lineno):
    """Parse one `类型=峰值 频率=5725Hz Gain=1.621549 Q=5` style line."""
    vals = {}
    for token in line.split():
        m = re.match(r"^([^=]+)=(.+)$", token)
        if not m:
            raise ApiError(f"第 {lineno} 行无法解析: {line}", 400)
        key, value = m.group(1).strip(), m.group(2).strip()
        if key == "类型":
            if value not in TYPE_MAP_CN:
                raise ApiError(f"第 {lineno} 行无法解析: 未知滤波类型 \"{value}\"", 400)
            vals["type"] = TYPE_MAP_CN[value]
        elif key == "频率":
            fm = re.match(r"^(\d+(?:\.\d+)?)\s*Hz?$", value, re.IGNORECASE)
            if not fm:
                raise ApiError(f"第 {lineno} 行无法解析: 频率 \"{value}\"", 400)
            vals["freq"] = float(fm.group(1))
        elif key == "Gain":
            try:
                vals["gain"] = float(value)
            except ValueError:
                raise ApiError(f"第 {lineno} 行无法解析: Gain \"{value}\"", 400)
        elif key == "Q":
            try:
                vals["q"] = float(value)
            except ValueError:
                raise ApiError(f"第 {lineno} 行无法解析: Q \"{value}\"", 400)
        else:
            raise ApiError(f"第 {lineno} 行无法解析: 未知字段 \"{key}\"", 400)
    missing = [k for k in ("type", "freq", "gain", "q") if k not in vals]
    if missing:
        raise ApiError(f"第 {lineno} 行无法解析: 缺少字段 {', '.join(missing)}", 400)
    return vals


def _peaking_coeffs(fs, f0, gdb, q):
    A = 10.0 ** (gdb / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2.0 * q)
    cw = math.cos(w0)
    b0, b1, b2 = 1.0 + alpha * A, -2.0 * cw, 1.0 - alpha * A
    a0, a1, a2 = 1.0 + alpha / A, -2.0 * cw, 1.0 - alpha / A
    return (b0 / a0, b1 / a0, b2 / a0), (1.0, a1 / a0, a2 / a0)


def _mag_db(ba, f, fs):
    b0, b1, b2 = ba[0]
    _, a1, a2 = ba[1]
    w = 2.0 * math.pi * f / fs
    z1 = complex(math.cos(w), -math.sin(w))
    num = b0 + b1 * z1 + b2 * z1 * z1
    den = 1.0 + a1 * z1 + a2 * z1 * z1
    return 20.0 * math.log10(abs(num / den))


def suggest_preamp(bands):
    """Worst-case combined boost across 10Hz-20kHz at 44.1k and 48k.

    Returns (suggested_preamp_db, warnings). Never underestimates.
    """
    warnings = []
    non_peaking = sorted({b["type"] for b in bands} - {"peaking"})
    if non_peaking:
        # Conservative upper bound: sum of positive gains (capped at +24 dB).
        est = min(sum(b["gain"] for b in bands if b["gain"] > 0), 24.0)
        warnings.append(
            "包含非峰值滤波器 (" + ", ".join(non_peaking) + ")，预增益为保守估算"
        )
        return -math.ceil(est * 10.0) / 10.0, warnings
    worst = 0.0
    for fs in (44100.0, 48000.0):
        cs = [_peaking_coeffs(fs, b["freq"], b["gain"], b["q"]) for b in bands]
        f = 10.0
        lim = min(20000.0, fs * 0.45)
        while f <= lim:
            t = sum(_mag_db(c, f, fs) for c in cs)
            if t > worst:
                worst = t
            f *= 1.0005
    return -math.ceil(worst * 10.0) / 10.0, warnings


def parse_eq_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ApiError("文本为空", 400)
    bands = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        bands.append(_parse_filter_line(line, lineno))
    if not (MIN_BANDS <= len(bands) <= MAX_BANDS):
        raise ApiError(f"波段数量必须在 {MIN_BANDS}~{MAX_BANDS} 之间, 实际 {len(bands)}", 400)
    for i, b in enumerate(bands, 1):
        if not (10.0 <= b["freq"] <= 22000.0):
            raise ApiError(f"第 {i} 段频率超出范围 (10 ~ 22000 Hz)", 400)
        if not (0.1 <= b["q"] <= 20.0):
            raise ApiError(f"第 {i} 段 Q 超出范围 (0.1 ~ 20)", 400)
        if not (-24.0 <= b["gain"] <= 24.0):
            raise ApiError(f"第 {i} 段增益超出范围 (-24 ~ +24 dB)", 400)
    preamp, warnings = suggest_preamp(bands)
    return {"bands": bands, "count": len(bands), "preamp_suggested_db": preamp,
            "warnings": warnings}


# ---------------------------------------------------------------- create/delete

def _sanitize_name(name):
    name = (name or "").strip().replace("/", "")
    name = re.sub(r"\s+", "-", name)
    name = name[:50].strip("-")
    return name or "preset"


def _ascii_slug(name):
    slug = name.strip().lower().replace(" ", "-")
    slug = re.sub(r"[^a-z0-9-]+", "", slug).strip("-")[:40]
    return slug


def _render_conf(description, stem, preamp_db, bands):
    n = len(bands)
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
        "#   - Web 控制面板: http://127.0.0.1:8787",
        "",
        "context.modules = [",
        "  { name = libpipewire-module-filter-chain",
        "    args = {",
        f'      node.description = "{description}"',
        f'      media.name       = "{description}"',
        "      filter.graph = {",
        "        nodes = [",
    ]
    for i, b in enumerate(bands, 1):
        label = TYPE_TO_LABEL[b["type"]]
        lines.append(
            f'          {{ type = builtin label = {label} name = f{i:02d} '
            f'control = {{ "Freq" = {b["freq"]:.1f}  "Q" = {b["q"]:.3f} '
            f'"Gain" = {b["gain"]: .3f} }} }}'
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
        "        audio.position = [ FL FR ]",
        "      }",
        "      playback.props = {",
        f'        node.name      = "effect_output.{stem}"',
        "        node.passive   = true",
        "        audio.channels = 2",
        "        audio.position = [ FL FR ]",
        "      }",
        "    }",
        "  }",
        "]",
        "",
    ]
    return "\n".join(lines)


def _existing_node_names():
    names = set()
    for f in list_preset_files():
        try:
            info = read_preset(f)
        except ApiError:
            continue
        if info["node_name"]:
            names.add(info["node_name"])
    return names


def create_preset(body):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("缺少预设名称", 400)
    preamp, bands = _validate_eq(body)
    clean = _sanitize_name(name)
    files = list_preset_files()
    nums = []
    for f in files:
        m = re.match(r"^(\d+)-", f.name)
        if m:
            nums.append(int(m.group(1)))
    nn = (max(nums) + 1) if nums else 60

    base = f"{nn:02d}-{clean}"
    target = EQ_DIR / f"{base}.conf"
    suffix = 2
    while target.exists():
        target = EQ_DIR / f"{base}-{suffix}.conf"
        suffix += 1

    existing = _existing_node_names()
    slug = _ascii_slug(name)
    stem = f"eq_{slug}" if slug else f"eq{nn:02d}"
    final_stem = stem
    sfx = 2
    while f"effect_input.{final_stem}" in existing:
        final_stem = f"{stem}-{sfx}"
        sfx += 1

    text = _render_conf(name.strip(), final_stem, preamp, bands)
    EQ_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".conf.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    must_run(["systemctl", "--user", "restart", SERVICE_UNIT])
    time.sleep(0.5)
    repair_links()
    return {"file": target.name, "description": name.strip(), "ok": True}


def delete_preset(body):
    p = preset_path(body.get("file"))
    files = list_preset_files()
    if len(files) <= 1:
        raise ApiError("至少保留一个预设，无法删除", 400)
    node_name = ""
    try:
        node_name = read_preset(p)["node_name"]
    except ApiError:
        pass
    p.unlink()
    forget_preset_volume(node_name)
    forget_family_volume(node_name)
    must_run(["systemctl", "--user", "restart", SERVICE_UNIT])
    time.sleep(0.5)
    repair_links()
    return {"ok": True}


# ---------------------------------------------------------------- routes

def pick_eq_sink(sinks):
    """Last-used EQ sink, else first preset's sink, else any EQ sink."""
    eqs = eq_sinks(sinks)
    if not eqs:
        return None
    state = load_state()
    last = state.get("last_eq_default") or {}
    if isinstance(last.get("id"), int):
        for s in eqs:
            if s["id"] == last["id"]:
                return s
    for f in list_preset_files():
        try:
            info = read_preset(f)
        except ApiError:
            continue
        for s in eqs:
            if s["name"] == info["node_name"]:
                return s
    return eqs[0]


def pick_physical_sink(sinks):
    """Last-used physical sink, else the first physical one."""
    state = load_state()
    last = state.get("last_non_eq_default") or {}
    if isinstance(last.get("id"), int) and not is_eq_name(last.get("name") or ""):
        for s in sinks:
            if s["id"] == last["id"] and not is_eq_name(s["name"]):
                return s
    phys = physical_sinks(sinks)
    return phys[0] if phys else None


def record_default_choice(sink):
    state = load_state()
    if is_eq_name(sink["name"]):
        state["last_eq_default"] = {"id": sink["id"], "name": sink["name"]}
    else:
        state["last_non_eq_default"] = {"id": sink["id"], "name": sink["name"]}
    save_state(state)


def handle_post(path, body):
    if path == "/api/sink/default":
        try:
            sink_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise ApiError("id 必须是整数", 400)
        sinks = list_sinks()
        target = next((s for s in sinks if s["id"] == sink_id), None)
        if target is None:
            raise ApiError("设备不存在", 400)
        capture_eq_memory(sinks)
        capture_family_memory(sinks)
        set_default_sink(sink_id)
        record_default_choice(target)
        after_default_switch(target)
        return status_payload()

    if path == "/api/eq/toggle":
        on = body.get("on")
        if not isinstance(on, bool):
            raise ApiError("on 必须是布尔值", 400)
        sinks = list_sinks()
        target = pick_eq_sink(sinks) if on else pick_physical_sink(sinks)
        if target is None:
            if on:
                raise ApiError("EQ 虚拟声卡不存在 (filter-chain 未加载?)", 400)
            raise ApiError("找不到非 EQ 输出设备", 400)
        capture_eq_memory(sinks)
        capture_family_memory(sinks)
        set_default_sink(target["id"])
        record_default_choice(target)
        after_default_switch(target)
        return status_payload()

    if path == "/api/eq":
        file = body.get("file")
        target = preset_path(file) if file else EQ_CONF
        preamp, bands = _validate_eq(body)
        write_eq(target, preamp, bands)
        must_run(["systemctl", "--user", "restart", SERVICE_UNIT])
        time.sleep(0.5)
        repair_links()
        return read_preset(target)

    if path == "/api/eq/parse":
        return parse_eq_text(body.get("text"))

    if path == "/api/eq/create":
        return create_preset(body)

    if path == "/api/eq/delete":
        return delete_preset(body)

    if path == "/api/service":
        action = body.get("action")
        if action not in ("start", "stop", "restart"):
            raise ApiError("action 必须是 start/stop/restart", 400)
        must_run(["systemctl", "--user", action, SERVICE_UNIT])
        if action in ("start", "restart"):
            time.sleep(0.5)
            repair_links()
        return {"ok": True, "eq_service": service_status()}

    if path == "/api/repair":
        return repair_links()

    if path == "/api/volume":
        scope = body.get("scope")
        if scope not in (None, "eq", "physical"):
            raise ApiError("scope 必须是 eq 或 physical", 400)
        value = None
        if "value" in body and body["value"] is not None:
            try:
                value = float(body["value"])
            except (TypeError, ValueError):
                raise ApiError("value 必须是数字", 400)
            if not (0.0 <= value <= 1.5):
                raise ApiError("音量超出范围 (0 ~ 1.5)", 400)
        muted = None
        if "muted" in body and body["muted"] is not None:
            if not isinstance(body["muted"], bool):
                raise ApiError("muted 必须是布尔值", 400)
            muted = body["muted"]
        if value is None and muted is None:
            raise ApiError("需要 value 或 muted 字段", 400)
        sinks = list_sinks()
        default_name, _ = default_node_name()
        via_eq = is_eq_name(default_name)
        eq_sink = None
        if via_eq:
            eq_sink = next((s for s in sinks if s["name"] == default_name), None)
        if scope == "eq" and not via_eq:
            raise ApiError("当前默认输出不是 EQ 设备", 400)
        if via_eq:
            physical = downstream_of(default_name, parse_pw_links(), sinks)
            if physical is None:
                phys = physical_sinks(sinks)
                physical = phys[0] if phys else None
        else:
            physical = next((s for s in sinks if s["name"] == default_name), None)
        if value is not None:
            if scope == "eq":
                if eq_sink is None:
                    raise ApiError("找不到 EQ 设备", 500)
                must_run(["wpctl", "set-volume", str(eq_sink["id"]),
                          f"{value:.4f}"])
                state = load_state()
                state.setdefault("preset_volumes", {})[eq_sink["name"]] = round(value, 4)
                save_state(state)
            else:
                if physical is None:
                    raise ApiError("找不到物理输出设备", 500)
                must_run(["wpctl", "set-volume", str(physical["id"]),
                          f"{value:.4f}"])
                state = load_state()
                state.setdefault("family_volumes", {})[default_name] = round(value, 4)
                save_state(state)
        if muted is not None:
            if scope is None:
                targets = ([eq_sink] if eq_sink is not None else [])
                if physical is not None:
                    targets.append(physical)
            elif scope == "eq":
                targets = [eq_sink] if eq_sink is not None else []
            else:
                targets = [physical] if physical is not None else []
            if not targets:
                raise ApiError("找不到输出设备", 500)
            for t in targets:
                must_run(["wpctl", "set-mute", str(t["id"]),
                          "1" if muted else "0"])
        return get_volume(sinks)

    raise ApiError("未知接口", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "eq-web/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # keep journal quiet; errors still surface via tracebacks

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, fname):
        path = (STATIC_DIR / fname).resolve()
        if not str(path).startswith(str(STATIC_DIR)) or not path.is_file():
            self._send_json({"error": "Not Found"}, 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(path.suffix, "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        try:
            with _REQ_LOCK:
                self._handle_locked(method)
        except ApiError as e:
            self._send_json({"error": str(e)}, e.status)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 - top-level guard
            try:
                self._send_json({"error": f"服务器内部错误: {e}"}, 500)
            except Exception:
                pass

    def _handle_locked(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET":
            if path == "/api/status":
                return self._send_json(status_payload())
            if path == "/api/eqs":
                return self._send_json(list_presets())
            if path == "/api/eq":
                file = (query.get("file") or [None])[0]
                if file:
                    return self._send_json(read_preset(preset_path(file)))
                files = list_preset_files()
                if not files:
                    raise ApiError("未找到任何预设文件", 404)
                return self._send_json(read_preset(files[0]))
            if path == "/api/volume":
                return self._send_json(get_volume())
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/" or path == "/index.html":
                return self._send_file("index.html")
            if path.startswith("/static/"):
                return self._send_file(path[len("/static/"):])
            return self._send_json({"error": "Not Found"}, 404)
        # POST
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError("请求体过大", 400)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("请求体不是合法 JSON", 400)
        if not isinstance(body, dict):
            raise ApiError("请求体必须是 JSON 对象", 400)
        return self._send_json(handle_post(path, body))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    try:
        time.sleep(0.8)  # let PipeWire/WirePlumber settle after service start
        repair_links()
    except Exception:  # noqa: BLE001 - startup repair is best-effort
        pass
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"eq-web listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
