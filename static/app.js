/* eq-web frontend · vanilla JS, no external deps */
"use strict";

(() => {
  // ---------- helpers ----------
  const $ = (id) => document.getElementById(id);

  const el = {
    svcBadge: $("svc-badge"),
    svcText: $("svc-text"),
    eqToggle: $("eq-toggle"),
    toggleState: $("toggle-state"),
    toggleDevice: $("toggle-device"),
    deviceList: $("device-list"),
    presetList: $("preset-list"),
    presetNew: $("preset-new"),
    presetImport: $("preset-import"),
    presetDelete: $("preset-delete"),
    editorTitle: $("editor-title"),
    presetName: $("preset-name"),
    modalRoot: $("modal-root"),
    modalTitle: $("modal-title"),
    modalClose: $("modal-close"),
    modalBackdrop: $("modal-backdrop"),
    modalNameRow: $("modal-name-row"),
    modalName: $("modal-name"),
    modalText: $("modal-text"),
    modalParse: $("modal-parse"),
    modalResult: $("modal-result"),
    modalPreamp: $("modal-preamp"),
    modalCreate: $("modal-create"),
    modalFill: $("modal-fill"),
    volValue: $("vol-value"),
    volEqRow: $("vol-eq-row"),
    volEqName: $("vol-eq-name"),
    volEqVal: $("vol-eq-val"),
    volSliderEq: $("vol-slider-eq"),
    volPhysLabel: $("vol-phys-label"),
    volPhysDesc: $("vol-phys-desc"),
    volPhysVal: $("vol-phys-val"),
    volSliderPhys: $("vol-slider-phys"),
    volVia: $("vol-via"),
    volViaName: $("vol-via-name"),
    muteBtn: $("mute-btn"),
    repairBtn: $("repair-btn"),
    preamp: $("preamp"),
    bandBody: $("band-body"),
    applyBtn: $("apply-btn"),
    revertBtn: $("revert-btn"),
    toastRoot: $("toast-root"),
    curve: $("curve"),
  };

  async function api(path, opts) {
    const res = await fetch(path, opts);
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      /* non-JSON error body */
    }
    if (!res.ok) {
      throw new Error((data && data.error) || `请求失败 (${res.status})`);
    }
    return data;
  }

  function toast(msg, isError) {
    const t = document.createElement("div");
    t.className = "toast" + (isError ? " toast-error" : "");
    t.textContent = msg;
    el.toastRoot.appendChild(t);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => t.remove(), 300);
    }, isError ? 4000 : 3000);
  }

  function guard(fn) {
    return async (...args) => {
      try {
        await fn(...args);
      } catch (e) {
        toast(e.message || String(e), true);
      }
    };
  }

  // ---------- state ----------
  let status = null;
  let eqData = null; // {file, description, preamp_db, bands:[{freq,q,gain,type}]}
  let presets = []; // from /api/eqs
  let currentFile = null; // preset file being edited
  let parsedResult = null; // successful /api/eq/parse result in modal
  let applying = false;

  // ---------- status rendering ----------
  const SVC_LABEL = {
    active: "服务运行中",
    inactive: "服务已停止",
    failed: "服务失败",
    activating: "服务启动中",
    unknown: "服务状态未知",
  };

  function renderStatus() {
    if (!status) return;
    const s = status.eq_service;
    el.svcBadge.className =
      "badge " + (s === "active" ? "badge-green" : s === "failed" ? "badge-red" : "badge-gray");
    el.svcText.textContent = SVC_LABEL[s] || s;

    const isEqDefault = !!(status.default_sink && status.default_sink.is_eq);
    el.eqToggle.setAttribute("aria-checked", isEqDefault ? "true" : "false");
    el.toggleState.textContent = isEqDefault ? "已开启" : "已关闭";
    el.toggleState.className = "toggle-state" + (isEqDefault ? " on" : " off");
    el.toggleDevice.textContent = status.default_sink
      ? status.default_sink.description || status.default_sink.name
      : "—";

    // device list
    el.deviceList.innerHTML = "";
    for (const d of status.sinks) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "device-item" + (d.is_default ? " is-default" : "");
      const body = document.createElement("span");
      body.className = "d-body";
      const name = document.createElement("span");
      name.className = "d-name";
      name.textContent = d.description || d.name;
      const sub = document.createElement("span");
      sub.className = "d-sub";
      sub.textContent = d.name;
      body.append(name, sub);
      btn.appendChild(body);
      if (d.is_eq) {
        const tag = document.createElement("span");
        tag.className = "tag tag-eq";
        tag.textContent = "EQ";
        btn.appendChild(tag);
      }
      if (d.is_default) {
        const tag = document.createElement("span");
        tag.className = "tag tag-default";
        tag.textContent = "默认";
        btn.appendChild(tag);
      }
      btn.addEventListener("click", guard(async () => {
        await api("/api/sink/default", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: d.id }),
        });
        await refreshStatus();
      }));
      li.appendChild(btn);
      el.deviceList.appendChild(li);
    }

    // volume (skip rows currently being dragged)
    if (status.volume) {
      const v = status.volume;
      if (!volDrag.eq && !volDrag.physical) {
        const pct = Math.round(v.value * 100);
        el.volValue.textContent = v.muted ? `${pct}%（静音）` : `${pct}%`;
      }
      const via = !!v.via_eq;
      el.volEqRow.classList.toggle("hidden", !via);
      el.volPhysLabel.classList.toggle("hidden", !via);
      el.volVia.classList.toggle("hidden", !(via && v.physical));
      if (via && v.physical) {
        el.volViaName.textContent = v.physical.description || v.physical.name;
      }
      if (via && v.eq) {
        el.volEqName.textContent = v.eq.description || v.eq.name;
        if (!volDrag.eq) setRowVolume(el.volSliderEq, el.volEqVal, v.eq);
      }
      if (v.physical && !volDrag.physical) {
        el.volPhysDesc.textContent = v.physical.description || v.physical.name;
        setRowVolume(el.volSliderPhys, el.volPhysVal, v.physical);
      }      el.muteBtn.classList.toggle("is-muted", !!v.muted);
      el.muteBtn.textContent = v.muted ? "取消静音" : "静音";
    }
  }

  function setRowVolume(slider, valEl, info) {
    const pct = Math.round(info.value * 100);
    valEl.textContent = info.muted ? `${pct}%（静音）` : `${pct}%`;
    slider.value = String(pct);
    slider.style.setProperty("--fill", pct + "%");
  }

  async function refreshStatus() {
    status = await api("/api/status");
    renderStatus();
  }

  // ---------- toggle ----------
  el.eqToggle.addEventListener("click", guard(async () => {
    const on = el.eqToggle.getAttribute("aria-checked") !== "true";
    el.eqToggle.disabled = true;
    try {
      status = await api("/api/eq/toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on }),
      });
      renderStatus();
    } finally {
      el.eqToggle.disabled = false;
    }
  }));

  // ---------- volume ----------
  const volDrag = { eq: false, physical: false };

  function bindVolSlider(slider, valEl, scope) {
    let timer = null;
    slider.addEventListener("input", () => {
      volDrag[scope] = true;
      const pct = Number(slider.value);
      slider.style.setProperty("--fill", pct + "%");
      valEl.textContent = `${pct}%`;
      if (scope === "physical" && status.volume && !status.volume.via_eq) {
        el.volValue.textContent = `${pct}%`;
      }
      clearTimeout(timer);
      timer = setTimeout(guard(async () => {
        try {
          status.volume = await api("/api/volume", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ scope, value: pct / 100 }),
          });
        } finally {
          volDrag[scope] = false;
        }
        renderStatus();
      }), 120);
    });
  }

  bindVolSlider(el.volSliderEq, el.volEqVal, "eq");
  bindVolSlider(el.volSliderPhys, el.volPhysVal, "physical");

  el.muteBtn.addEventListener("click", guard(async () => {
    const muted = !(status && status.volume && status.volume.muted);
    status.volume = await api("/api/volume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ muted }),
    });
    renderStatus();
  }));

  // ---------- link repair ----------
  el.repairBtn.addEventListener("click", guard(async () => {
    el.repairBtn.disabled = true;
    try {
      const r = await api("/api/repair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (r.links_clean) {
        toast(`链路正常（断开 ${r.fixed.disconnected} · 重连 ${r.fixed.reconnected}）`);
      } else {
        toast("修复后仍有异常链路，可稍后再试一次", true);
      }
    } finally {
      el.repairBtn.disabled = false;
    }
  }));

  // ---------- service buttons ----------
  document.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", guard(async () => {
      const action = btn.dataset.action;
      btn.disabled = true;
      try {
        await api("/api/service", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action }),
        });
        await new Promise((r) => setTimeout(r, 400));
        await refreshStatus();
      } finally {
        btn.disabled = false;
      }
    }));
  });

  // ---------- preset management ----------

  async function refreshPresets() {
    const data = await api("/api/eqs");
    presets = data.presets || [];
    if (!currentFile && presets.length) currentFile = presets[0].file;
    renderPresets();
  }

  function renderPresets() {
    el.presetList.innerHTML = "";
    for (const p of presets) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "preset-item" + (p.file === currentFile ? " is-current" : "");
      btn.title = p.file;
      const body = document.createElement("span");
      body.className = "p-body";
      const name = document.createElement("span");
      name.className = "p-name";
      name.textContent = p.description || p.file;
      const sub = document.createElement("span");
      sub.className = "p-sub";
      sub.textContent = p.file;
      body.append(name, sub);
      const bands = document.createElement("span");
      bands.className = "tag-bands";
      bands.textContent = `${p.bands} 段`;
      btn.append(body, bands);
      if (p.sink && p.sink.loaded) {
        const loaded = document.createElement("span");
        loaded.className = "p-loaded";
        loaded.textContent = "已加载";
        const dot = document.createElement("span");
        dot.className = "dot";
        loaded.prepend(dot);
        btn.appendChild(loaded);
      }
      btn.addEventListener("click", guard(() => selectPreset(p.file)));
      li.appendChild(btn);
      el.presetList.appendChild(li);
    }
  }

  async function loadPreset(file) {
    eqData = await api("/api/eq?file=" + encodeURIComponent(file));
    currentFile = eqData.file || file;
    renderPresets();
    buildEditor();
    await updateCurve();
  }

  async function selectPreset(file) {
    try {
      await loadPreset(file);
    } catch (e) {
      toast(e.message || String(e), true);
    }
  }

  let deleteArmed = false;
  let deleteTimer = null;

  function disarmDelete() {
    deleteArmed = false;
    clearTimeout(deleteTimer);
    el.presetDelete.textContent = "删除";
    el.presetDelete.classList.remove("btn-confirm");
  }

  el.presetDelete.addEventListener("click", guard(async () => {
    if (!currentFile) {
      toast("没有可删除的预设", true);
      return;
    }
    if (!deleteArmed) {
      deleteArmed = true;
      el.presetDelete.textContent = "确认删除";
      el.presetDelete.classList.add("btn-confirm");
      deleteTimer = setTimeout(disarmDelete, 3500);
      return;
    }
    disarmDelete();
    const target = currentFile;
    await api("/api/eq/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: target }),
    });
    currentFile = null;
    await refreshPresets();
    if (presets.length) await loadPreset(presets[0].file);
    toast("已删除预设（音频会闪断一下）");
    setTimeout(guard(refreshStatus), 1500);
  }));

  let modalMode = "create";
  function openModal(mode) {
    modalMode = mode;
    parsedResult = null;
    el.modalRoot.classList.remove("hidden");
    el.modalRoot.setAttribute("aria-hidden", "false");
    el.modalTitle.textContent = mode === "create" ? "新建预设" : "导入文本 EQ";
    el.modalNameRow.style.display = mode === "create" ? "" : "none";
    el.modalCreate.style.display = mode === "create" ? "" : "none";
    el.modalFill.style.display = mode === "import" ? "" : "none";
    el.modalCreate.disabled = true;
    el.modalFill.disabled = true;
    el.modalText.value = "";
    el.modalName.value = "";
    el.modalPreamp.value = "0";
    setModalResult("", null);
    if (mode === "create") el.modalName.focus();
    else el.modalText.focus();
  }

  function closeModal() {
    el.modalRoot.classList.add("hidden");
    el.modalRoot.setAttribute("aria-hidden", "true");
  }

  function setModalResult(text, kind) {
    el.modalResult.textContent = text;
    el.modalResult.className = "modal-result" + (kind ? " " + kind : "");
  }

  el.presetNew.addEventListener("click", () => openModal("create"));
  el.presetImport.addEventListener("click", () => openModal("import"));
  el.modalClose.addEventListener("click", closeModal);
  el.modalBackdrop.addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el.modalRoot.classList.contains("hidden")) closeModal();
  });

  el.modalParse.addEventListener("click", guard(async () => {
    setModalResult("解析中…", null);
    try {
      parsedResult = await api("/api/eq/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: el.modalText.value }),
      });
      el.modalPreamp.value = parsedResult.preamp_suggested_db;
      let msg = `已解析 ${parsedResult.count} 段 · 建议预增益 ${parsedResult.preamp_suggested_db} dB（可修改）`;
      if (parsedResult.warnings && parsedResult.warnings.length) {
        msg += " · " + parsedResult.warnings.join("；");
      }
      setModalResult(msg, "ok");
      el.modalCreate.disabled = false;
      el.modalFill.disabled = false;
    } catch (e) {
      parsedResult = null;
      el.modalCreate.disabled = true;
      el.modalFill.disabled = true;
      setModalResult(e.message || String(e), "err");
    }
  }));

  el.modalCreate.addEventListener("click", guard(async () => {
    if (!parsedResult) return;
    const name = el.modalName.value.trim();
    if (!name) {
      setModalResult("请填写预设名称", "err");
      return;
    }
    el.modalCreate.disabled = true;
    try {
      const resp = await api("/api/eq/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          preamp_db: Number(el.modalPreamp.value),
          bands: parsedResult.bands,
        }),
      });
      closeModal();
      toast(`已创建预设「${resp.description}」（音频会闪断一下）`);
      currentFile = resp.file;
      await refreshPresets();
      await loadPreset(resp.file);
      // new virtual sink shows up ~1s after the service restart
      setTimeout(guard(async () => {
        await refreshStatus();
        await refreshPresets();
      }), 1500);
      setTimeout(guard(async () => {
        await refreshStatus();
        await refreshPresets();
      }), 4000);
    } catch (e) {
      el.modalCreate.disabled = false;
      setModalResult(e.message || String(e), "err");
    }
  }));

  el.modalFill.addEventListener("click", () => {
    if (!parsedResult) return;
    eqData = {
      file: currentFile,
      description: currentFile,
      preamp_db: Number(el.modalPreamp.value),
      bands: parsedResult.bands.map((b) => ({ ...b })),
    };
    buildEditor();
    updateCurve();
    closeModal();
    toast("已填入编辑器，点击「应用」保存");
  });

  // ---------- band editor ----------
  const GAIN_SLIDER_MIN = -12;
  const GAIN_SLIDER_MAX = 12;

  function buildEditor() {
    el.bandBody.innerHTML = "";
    el.editorTitle.textContent = `均衡器参数（${eqData.bands.length} 段）`;
    el.presetName.textContent = eqData.description || "";
    el.bandBody.dataset.types = eqData.bands.map((b) => b.type).join(",");
    eqData.bands.forEach((b, i) => {
      const tr = document.createElement("tr");

      const tdIdx = document.createElement("td");
      tdIdx.innerHTML = `<span class="idx-num">${String(i + 1).padStart(2, "0")}</span>`;
      tr.appendChild(tdIdx);

      const tdFreq = document.createElement("td");
      const inFreq = document.createElement("input");
      inFreq.type = "number";
      inFreq.min = "10";
      inFreq.max = "22000";
      inFreq.step = "0.1";
      inFreq.value = b.freq;
      inFreq.title = `波段 ${i + 1} 频率`;
      inFreq.addEventListener("input", () => {
        b.freq = Number(inFreq.value) || b.freq;
        scheduleCurve();
      });
      tdFreq.appendChild(inFreq);
      tr.appendChild(tdFreq);

      const tdQ = document.createElement("td");
      const inQ = document.createElement("input");
      inQ.type = "number";
      inQ.min = "0.1";
      inQ.max = "20";
      inQ.step = "0.01";
      inQ.value = b.q;
      inQ.title = `波段 ${i + 1} Q 值`;
      inQ.addEventListener("input", () => {
        b.q = Number(inQ.value) || b.q;
        scheduleCurve();
      });
      tdQ.appendChild(inQ);
      tr.appendChild(tdQ);

      const tdGain = document.createElement("td");
      tdGain.className = "c-gain";
      const wrap = document.createElement("div");
      wrap.className = "gain-cell";
      const slider = document.createElement("input");
      slider.type = "range";
      slider.className = "slider";
      slider.min = String(GAIN_SLIDER_MIN);
      slider.max = String(GAIN_SLIDER_MAX);
      slider.step = "0.1";
      slider.value = b.gain;
      slider.title = `波段 ${i + 1} 增益`;
      const num = document.createElement("input");
      num.type = "number";
      num.min = "-24";
      num.max = "24";
      num.step = "0.1";
      num.value = b.gain;
      const syncGain = (v) => {
        b.gain = v;
        slider.value = String(Math.min(GAIN_SLIDER_MAX, Math.max(GAIN_SLIDER_MIN, v)));
        num.value = String(v);
        slider.style.setProperty("--fill", ((v - GAIN_SLIDER_MIN) / (GAIN_SLIDER_MAX - GAIN_SLIDER_MIN)) * 100 + "%");
        scheduleCurve();
      };
      slider.addEventListener("input", () => syncGain(Number(slider.value)));
      num.addEventListener("input", () => {
        const v = Number(num.value);
        if (Number.isFinite(v)) syncGain(v);
      });
      syncGain(b.gain);
      wrap.append(slider, num);
      tdGain.appendChild(wrap);
      tr.appendChild(tdGain);

      el.bandBody.appendChild(tr);
    });
    el.preamp.value = eqData.preamp_db;
  }

  function currentPayload() {
    return {
      file: currentFile || undefined,
      preamp_db: Number(el.preamp.value),
      bands: eqData.bands.map((b) => ({ freq: b.freq, q: b.q, gain: b.gain, type: b.type })),
    };
  }

  el.applyBtn.addEventListener("click", guard(async () => {
    if (applying) return;
    applying = true;
    el.applyBtn.classList.add("is-loading");
    el.applyBtn.textContent = "应用中…";
    el.applyBtn.disabled = true;
    try {
      eqData = await api("/api/eq", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(currentPayload()),
      });
      currentFile = eqData.file || currentFile;
      buildEditor();
      drawCurve();
      toast("已应用（音频会闪断一下）");
      await refreshStatus();
      await refreshPresets();
    } finally {
      applying = false;
      el.applyBtn.classList.remove("is-loading");
      el.applyBtn.textContent = "应用";
      el.applyBtn.disabled = false;
    }
  }));

  el.revertBtn.addEventListener("click", guard(async () => {
    eqData = await api("/api/eq" + (currentFile ? "?file=" + encodeURIComponent(currentFile) : ""));
    buildEditor();
    drawCurve();
    toast("已还原为当前配置");
  }));

  // ---------- curve ----------
  let offlineCtx = null;

  function logX(f) {
    return Math.log(f / 20) / Math.log(1000); // 0..1 across 20Hz..20kHz
  }

  const FREQ_TICKS = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];
  const TICK_LABEL = { 1000: "1k", 2000: "2k", 5000: "5k", 10000: "10k", 20000: "20k" };
  const DB_MIN = -24;
  const DB_MAX = 12;

  function drawCurve() {
    const canvas = el.curve;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 600;
    const h = 260;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const padL = 40, padR = 12, padT = 12, padB = 24;
    const plotW = w - padL - padR;
    const plotH = h - padT - padB;
    const x = (f) => padL + logX(f) * plotW;
    const y = (db) => padT + ((DB_MAX - db) / (DB_MAX - DB_MIN)) * plotH;

    const css = getComputedStyle(document.documentElement);
    const cBorder = css.getPropertyValue("--border").trim();
    const cText = css.getPropertyValue("--text-3").trim();
    const cAccent = css.getPropertyValue("--accent").trim();
    const cZero = "rgba(255,255,255,0.18)";

    // dB grid
    ctx.font = "10px " + css.getPropertyValue("--font-ui");
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let db = DB_MIN; db <= DB_MAX; db += 6) {
      const yy = y(db);
      ctx.strokeStyle = db === 0 ? cZero : cBorder;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, yy);
      ctx.lineTo(w - padR, yy);
      ctx.stroke();
      ctx.fillStyle = cText;
      ctx.fillText(`${db > 0 ? "+" : ""}${db}`, padL - 6, yy);
    }
    // freq ticks
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const f of FREQ_TICKS) {
      const xx = x(f);
      ctx.strokeStyle = cBorder;
      ctx.beginPath();
      ctx.moveTo(xx, padT);
      ctx.lineTo(xx, h - padB);
      ctx.stroke();
      ctx.fillStyle = cText;
      ctx.fillText(TICK_LABEL[f] || String(f), xx, h - padB + 6);
    }

    if (!curvePoints) return;

    // curve + subtle fill
    const pts = curvePoints; // [{f, db}]
    ctx.save();
    ctx.beginPath();
    pts.forEach((p, i) => {
      const xx = x(p.f), yy = y(Math.max(DB_MIN, Math.min(DB_MAX, p.db)));
      if (i === 0) ctx.moveTo(xx, yy);
      else ctx.lineTo(xx, yy);
    });
    ctx.strokeStyle = cAccent;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.lineTo(x(20000), y(0));
    ctx.lineTo(x(20), y(0));
    ctx.closePath();
    ctx.fillStyle = "rgba(34, 211, 238, 0.07)";
    ctx.fill();
    ctx.restore();
  }

  let curvePoints = null;
  let curveTimer = null;

  function scheduleCurve() {
    clearTimeout(curveTimer);
    curveTimer = setTimeout(updateCurve, 50);
  }

  const WEB_AUDIO_TYPE = { peaking: "peaking", lowshelf: "lowshelf", highshelf: "highshelf" };

  async function updateCurve() {
    const payload = currentPayload();
    const N = 400;
    const freqs = new Float32Array(N);
    for (let i = 0; i < N; i++) freqs[i] = 20 * Math.pow(1000, i / (N - 1));
    try {
      if (!offlineCtx) {
        offlineCtx = new OfflineAudioContext(1, 128, 48000);
      }
      const mag = new Float32Array(N).fill(1);
      const phase = new Float32Array(N);
      for (const b of payload.bands) {
        const node = offlineCtx.createBiquadFilter();
        node.type = WEB_AUDIO_TYPE[b.type] || "peaking";
        node.frequency.value = Math.min(24000, Math.max(10, b.freq));
        node.Q.value = Math.min(20, Math.max(0.1, b.q));
        node.gain.value = Math.min(24, Math.max(-24, b.gain));
        const m = new Float32Array(N);
        node.getFrequencyResponse(freqs, m, phase);
        for (let i = 0; i < N; i++) mag[i] *= m[i];
      }
      const pre = Math.pow(10, (Number(el.preamp.value) || 0) / 20);
      curvePoints = [];
      for (let i = 0; i < N; i++) {
        curvePoints.push({ f: freqs[i], db: 20 * Math.log10(Math.max(1e-6, mag[i] * pre)) });
      }
    } catch (_) {
      return; // Web Audio unavailable; keep last curve
    }
    drawCurve();
  }

  window.addEventListener("resize", () => drawCurve());

  // ---------- init ----------
  (async () => {
    el.preamp.addEventListener("input", scheduleCurve);
    try {
      await refreshPresets();
      if (currentFile) await loadPreset(currentFile);
    } catch (e) {
      toast(e.message || String(e), true);
    }
    try {
      await refreshStatus();
    } catch (e) {
      toast(e.message || String(e), true);
    }
    if (new URLSearchParams(location.search).get("modal") === "create") {
      openModal("create");
    }
    // refresh status periodically (service badge, default sink drift)
    setInterval(guard(refreshStatus), 10000);
  })();
})();
