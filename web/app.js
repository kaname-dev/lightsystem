/* Light System Web UI */
(() => {
  const $ = (id) => document.getElementById(id);
  let state = null;
  let ws = null;
  let selectedCueIds = new Set();
  let zoom = 1;
  let viewStart = 0;
  let panDrag = false;
  let panAnchorX = 0;
  let panAnchorStart = 0;
  let seekDrag = false;
  const heldKeys = new Set();
  let editTileIndex = 0;
  let editChannels = [100, 128, 0, 0, 0, 64, 16, 63, 12, 0];
  let sidebarMode = "new"; // "new" | "edit"
  let tePalette = null;
  let blePalette = null;
  let esp32Palette = null;
  let esp32Selected = [];
  let esp32PaintTimer = null;
  let esp32SelAnchor = null;
  let esp32ActiveGroupSpans = null; // グループ選択時は絶対ピクセルで塗る
  let lastShownOverlapAlertKey = "";

  // サーバ未応答時でもボタンを出すためのフォールバック
  const ESP32_PATTERN_FALLBACK = [
    { id: "club_strobe_white", label: "白ストロボ", desc: "ブラインダー風の白点滅" },
    { id: "club_strobe_color", label: "カラーストロボ", desc: "メイン色の高速点滅" },
    { id: "club_blinder", label: "ブラインダー", desc: "白フラッシュ→減衰" },
    { id: "club_bass_pulse", label: "バスパルス", desc: "キックっぽい全体パルス" },
    { id: "club_buildup", label: "ビルドアップ", desc: "暗→徐々に明るく→ヒット" },
    { id: "club_drop", label: "ドロップ", desc: "黒→白ヒット→色残光" },
    { id: "club_uv_wash", label: "UVウォッシュ", desc: "紫〜青紫の全体洗い" },
    { id: "club_rgb_wash", label: "RGBウォッシュ", desc: "赤緑青の切り替え洗い" },
    { id: "club_halves", label: "ハーフ切替", desc: "左右で色が交互" },
    { id: "club_quarters", label: "4分割フラッシュ", desc: "4セクション巡回点灯" },
    { id: "club_tunnel", label: "トンネル", desc: "中央へ収束するリング" },
    { id: "club_expand", label: "エクスプロード", desc: "中央から外側へ爆発" },
    { id: "club_snake", label: "スネーク", desc: "複数ヘッドが這う" },
    { id: "club_rain", label: "レイン", desc: "上から落ちる光点" },
    { id: "club_heartbeat", label: "ハートビート", desc: "ドン・ドンの鼓動" },
    { id: "club_laser_dots", label: "レーザードット", desc: "細点の高速ラン" },
    { id: "club_sweep", label: "スイープ", desc: "太いビームが往復" },
    { id: "club_zone_chase", label: "ゾーンチェイス", desc: "ブロックが階段状に走る" },
    { id: "club_blackout_hit", label: "ブラックアウトヒット", desc: "ほぼ消灯→瞬間ヒット" },
    { id: "club_warm_cool", label: "ウォーム／クール", desc: "暖色と寒色の波" },
    { id: "club_magenta_teal", label: "マゼンタ／ティール", desc: "クラブ定番2色" },
    { id: "club_sparkle_dark", label: "ダークスパークル", desc: "暗い床にキラ" },
    { id: "club_roller", label: "ローラー", desc: "色帯がロール" },
    { id: "club_alert", label: "アラート", desc: "赤点滅警告風" },
    { id: "rainbow", label: "虹（固定）", desc: "全長レインボー" },
    { id: "rainbow_cycle", label: "虹サイクル", desc: "虹が流れる" },
    { id: "chase", label: "チェイス", desc: "色が走る" },
    { id: "theater", label: "シアター", desc: "点滅チェイス" },
    { id: "comet", label: "コメット", desc: "尾を引く光" },
    { id: "scanner", label: "スキャナー", desc: "往復ビーム" },
    { id: "breathe", label: "ブリーズ", desc: "全体が呼吸" },
    { id: "strobe", label: "ストロボ", desc: "点滅" },
    { id: "sparkle", label: "スパークル", desc: "きらめき" },
    { id: "fire", label: "炎", desc: "炎っぽい揺らぎ" },
    { id: "wave", label: "ウェーブ", desc: "正弦波の波" },
    { id: "stripes", label: "ストライプ", desc: "2色交互" },
    { id: "gradient", label: "グラデ", desc: "端から端へ色変化" },
    { id: "wipe", label: "ワイプ", desc: "塗りつぶし進行" },
    { id: "police", label: "ポリス", desc: "赤青交互" },
    { id: "aurora", label: "オーロラ", desc: "緑紫の揺らぎ" },
    { id: "ocean", label: "オーシャン", desc: "青緑の波" },
    { id: "candy", label: "キャンディ", desc: "ポップな多色" },
  ];
  const ESP32_COLOR_FALLBACK = [
    { name: "赤", r: 255, g: 0, b: 0 },
    { name: "緑", r: 0, g: 255, b: 0 },
    { name: "青", r: 0, g: 0, b: 255 },
    { name: "白", r: 255, g: 255, b: 255 },
    { name: "暖白", r: 255, g: 180, b: 100 },
    { name: "紫", r: 180, g: 0, b: 255 },
    { name: "黄", r: 255, g: 220, b: 0 },
    { name: "シアン", r: 0, g: 220, b: 255 },
    { name: "橙", r: 255, g: 120, b: 20 },
    { name: "ピンク", r: 255, g: 80, b: 160 },
  ];

  function luminance(r, g, b) {
    return 0.299 * r + 0.587 * g + 0.114 * b;
  }

  function renderCh9Swatches(containerId, hiddenId, labelId, selected, onPick) {
    const wrap = $(containerId);
    const hidden = $(hiddenId);
    if (!wrap || !hidden) return;
    const swatches = state?.dmx?.ch9_swatches || [];
    if (!wrap.dataset.built || Number(wrap.dataset.len) !== swatches.length) {
      wrap.innerHTML = "";
      swatches.forEach((s) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ch9-swatch";
        btn.title = s.label;
        btn.dataset.value = String(s.value);
        btn.textContent = s.short;
        btn.style.background = `rgb(${s.r},${s.g},${s.b})`;
        if (luminance(s.r, s.g, s.b) < 140) btn.classList.add("light-text");
        btn.addEventListener("click", () => {
          hidden.value = String(s.value);
          if (labelId && $(labelId)) $(labelId).textContent = `CH9 = ${s.value}（${s.short}）`;
          wrap.querySelectorAll(".ch9-swatch").forEach((el) => {
            el.classList.toggle("active", el.dataset.value === String(s.value));
          });
          if (onPick) onPick(s.value);
        });
        wrap.appendChild(btn);
      });
      wrap.dataset.built = "1";
      wrap.dataset.len = String(swatches.length);
    }
    const val = selected != null ? Number(selected) : Number(hidden.value);
    hidden.value = String(val);
    let matched = swatches.find((s) => s.value === val);
    if (!matched && swatches.length) {
      // 近い代表値をハイライト
      matched = swatches.reduce((a, b) =>
        Math.abs(a.value - val) <= Math.abs(b.value - val) ? a : b
      );
    }
    wrap.querySelectorAll(".ch9-swatch").forEach((el) => {
      el.classList.toggle("active", matched && el.dataset.value === String(matched.value));
    });
    if (labelId && $(labelId)) {
      $(labelId).textContent = matched
        ? `CH9 = ${val}（${matched.short}）`
        : `CH9 = ${val}`;
    }
  }

  function createLedPalette(svId, hueId, rId, gId, bId, swatchId, labelId, onChange) {
    const sv = $(svId);
    const hueEl = $(hueId);
    if (!sv || !hueEl) return null;
    const svCtx = sv.getContext("2d");
    const hueCtx = hueEl.getContext("2d");
    const statePal = { h: 0.05, s: 1, v: 1, dragging: null };

    function hsvToRgb(h, s, v) {
      const i = Math.floor(h * 6);
      const f = h * 6 - i;
      const p = v * (1 - s);
      const q = v * (1 - f * s);
      const t = v * (1 - (1 - f) * s);
      let r, g, b;
      switch (i % 6) {
        case 0: r = v; g = t; b = p; break;
        case 1: r = q; g = v; b = p; break;
        case 2: r = p; g = v; b = t; break;
        case 3: r = p; g = q; b = v; break;
        case 4: r = t; g = p; b = v; break;
        default: r = v; g = p; b = q; break;
      }
      return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
    }

    function rgbToHsv(r, g, b) {
      r /= 255; g /= 255; b /= 255;
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      const d = max - min;
      let h = 0;
      const s = max === 0 ? 0 : d / max;
      const v = max;
      if (d !== 0) {
        switch (max) {
          case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
          case g: h = ((b - r) / d + 2) / 6; break;
          default: h = ((r - g) / d + 4) / 6; break;
        }
      }
      return { h, s, v };
    }

    function drawHue() {
      const w = hueEl.width;
      const h = hueEl.height;
      for (let y = 0; y < h; y++) {
        const [r, g, b] = hsvToRgb(y / Math.max(1, h - 1), 1, 1);
        hueCtx.fillStyle = `rgb(${r},${g},${b})`;
        hueCtx.fillRect(0, y, w, 1);
      }
      const hy = statePal.h * (h - 1);
      hueCtx.strokeStyle = "#fff";
      hueCtx.lineWidth = 2;
      hueCtx.strokeRect(1, hy - 3, w - 2, 6);
    }

    function drawSv() {
      const w = sv.width;
      const h = sv.height;
      const img = svCtx.createImageData(w, h);
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
          const s = x / Math.max(1, w - 1);
          const v = 1 - y / Math.max(1, h - 1);
          const [r, g, b] = hsvToRgb(statePal.h, s, v);
          const i = (y * w + x) * 4;
          img.data[i] = r;
          img.data[i + 1] = g;
          img.data[i + 2] = b;
          img.data[i + 3] = 255;
        }
      }
      svCtx.putImageData(img, 0, 0);
      const cx = statePal.s * (w - 1);
      const cy = (1 - statePal.v) * (h - 1);
      svCtx.beginPath();
      svCtx.arc(cx, cy, 6, 0, Math.PI * 2);
      svCtx.strokeStyle = "#fff";
      svCtx.lineWidth = 2;
      svCtx.stroke();
      svCtx.beginPath();
      svCtx.arc(cx, cy, 6, 0, Math.PI * 2);
      svCtx.strokeStyle = "#000";
      svCtx.lineWidth = 1;
      svCtx.stroke();
    }

    function commit() {
      const [r, g, b] = hsvToRgb(statePal.h, statePal.s, statePal.v);
      if ($(rId)) $(rId).value = String(r);
      if ($(gId)) $(gId).value = String(g);
      if ($(bId)) $(bId).value = String(b);
      if (swatchId && $(swatchId)) $(swatchId).style.background = `rgb(${r},${g},${b})`;
      if (labelId && $(labelId)) $(labelId).textContent = `RGB ${r}, ${g}, ${b}`;
      drawHue();
      drawSv();
      if (typeof onChange === "function") onChange(r, g, b);
    }

    function setRgb(r, g, b) {
      const hsv = rgbToHsv(Number(r), Number(g), Number(b));
      statePal.h = hsv.h;
      statePal.s = hsv.s;
      statePal.v = hsv.v;
      commit();
    }

    function pickSv(e) {
      const rect = sv.getBoundingClientRect();
      const x = Math.min(sv.width - 1, Math.max(0, e.clientX - rect.left));
      const y = Math.min(sv.height - 1, Math.max(0, e.clientY - rect.top));
      statePal.s = x / Math.max(1, sv.width - 1);
      statePal.v = 1 - y / Math.max(1, sv.height - 1);
      commit();
    }

    function pickHue(e) {
      const rect = hueEl.getBoundingClientRect();
      const y = Math.min(hueEl.height - 1, Math.max(0, e.clientY - rect.top));
      statePal.h = y / Math.max(1, hueEl.height - 1);
      commit();
    }

    sv.addEventListener("pointerdown", (e) => {
      statePal.dragging = "sv";
      sv.setPointerCapture(e.pointerId);
      pickSv(e);
    });
    sv.addEventListener("pointermove", (e) => {
      if (statePal.dragging === "sv") pickSv(e);
    });
    sv.addEventListener("pointerup", () => { statePal.dragging = null; });
    hueEl.addEventListener("pointerdown", (e) => {
      statePal.dragging = "hue";
      hueEl.setPointerCapture(e.pointerId);
      pickHue(e);
    });
    hueEl.addEventListener("pointermove", (e) => {
      if (statePal.dragging === "hue") pickHue(e);
    });
    hueEl.addEventListener("pointerup", () => { statePal.dragging = null; });

    setRgb($(rId)?.value || 255, $(gId)?.value || 80, $(bId)?.value || 40);
    return { setRgb, commit };
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      let msg = res.statusText;
      try {
        const j = await res.json();
        msg = j.detail || j.error || JSON.stringify(j);
      } catch (_) {}
      throw new Error(msg);
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return null;
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      $("connStatus").textContent = "WebSocket 接続済";
    };
    ws.onclose = () => {
      $("connStatus").textContent = "切断 — 再接続中…";
      setTimeout(connectWs, 1500);
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.state) mergeState(msg.state);
        if (msg.event === "tick" && msg.position != null) {
          if (state?.timeline) state.timeline.position = msg.position;
          updateTimeLabel();
          drawTimeline();
        }
        if (msg.event === "hold" || msg.event === "cues" || msg.event === "timeline") {
          renderTiles();
          renderCues();
          renderEsp32OverlapWarnings();
          drawTimeline();
          updateTransport();
        }
        if (msg.event === "dmx" || msg.event === "hello") {
          renderDmx();
        }
        if (msg.event === "ble" || msg.event === "hello") {
          renderBle();
        }
        if (msg.event === "esp32" || msg.event === "hello") {
          renderEsp32();
          renderEsp32OverlapWarnings();
        }
        if (msg.event === "ai" || msg.event === "hello") {
          renderAi();
        }
        if (msg.event === "tiles" || msg.event === "hello") {
          renderTileEditor();
          renderTiles();
        }
        if (msg.event === "error" || msg.state?.last_error) {
          showError(msg.state?.last_error || state?.last_error);
        }
      } catch (_) {}
    };
  }

  function showError(msg) {
    const el = $("lastError");
    if (!msg) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = msg;
  }

  function mergeState(partial) {
    if (!state) state = partial;
    else {
      for (const k of Object.keys(partial)) {
        if (partial[k] && typeof partial[k] === "object" && !Array.isArray(partial[k])) {
          state[k] = { ...(state[k] || {}), ...partial[k] };
        } else {
          state[k] = partial[k];
        }
      }
    }
    if (state.timeline) {
      zoom = state.timeline.zoom || zoom;
      viewStart = state.timeline.view_start || viewStart;
    }
  }

  async function refreshState() {
    state = await api("/api/state");
    zoom = state.timeline?.zoom || 1;
    viewStart = state.timeline?.view_start || 0;
    renderAll();
  }

  function renderAll() {
    updateTransport();
    updateTimeLabel();
    renderTiles();
    renderCues();
    renderEsp32OverlapWarnings();
    renderDmx();
    renderBle();
    renderEsp32();
    renderAi();
    renderTileEditor();
    drawTimeline();
    $("zoomLabel").textContent = `${Math.round(zoom * 100)}%`;
    $("chkRecord").checked = !!state.timeline?.record;
    $("chkAutoplay").checked = state.timeline?.autoplay !== false;
    showError(state?.last_error);
  }

  function updateTransport() {
    const playing = !!state?.timeline?.playing;
    $("btnPlay").textContent = playing ? "⏸ 停止" : "▶ 再生";
  }

  function updateTimeLabel() {
    const d = state?.timeline?.duration || 0;
    const t = state?.timeline?.position || 0;
    $("timeLabel").textContent = `${t.toFixed(2)} / ${d.toFixed(2)} s`;
  }

  function renderTiles() {
    const grid = $("tileGrid");
    const tiles = state?.tiles || [];
    const holds = new Set(state?.holds || []);
    grid.innerHTML = "";
    tiles.forEach((tile, i) => {
      const el = document.createElement("div");
      el.className = "tile";
      const tags = [];
      if (tile.apply_laser !== false) tags.push("L");
      if (tile.apply_led) tags.push("LED");
      const hk = tile.hotkey ? `\n⌨ ${tile.hotkey}` : "";
      el.textContent = `${tags.length ? "[" + tags.join("+") + "] " : ""}${tile.title || "tile"}${hk}`;
      // highlight if any hold uses this tile title (approx)
      const active = [...holds].some((src) => {
        // held by ptr:i or key
        return src === `ptr:${i}` || (tile.hotkey && src === String(tile.hotkey).toLowerCase());
      });
      if (active) el.classList.add("active");

      const press = (src) => {
        api("/api/tiles/hold", {
          method: "POST",
          body: JSON.stringify({ source: src, tile_index: i }),
        }).catch(alert);
        el.classList.add("active");
      };
      const release = (src) => {
        api("/api/tiles/release", {
          method: "POST",
          body: JSON.stringify({ source: src }),
        }).catch(() => {});
        el.classList.remove("active");
      };

      el.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        el.setPointerCapture(e.pointerId);
        press(`ptr:${i}`);
      });
      el.addEventListener("pointerup", () => release(`ptr:${i}`));
      el.addEventListener("pointercancel", () => release(`ptr:${i}`));
      grid.appendChild(el);
    });
  }

  function overlappingCueIds() {
    const set = new Set();
    (state?.esp32_overlap_warnings || []).forEach((w) => {
      if (w.cue_a) set.add(w.cue_a);
      if (w.cue_b) set.add(w.cue_b);
    });
    return set;
  }

  function renderEsp32OverlapWarnings() {
    const box = $("esp32OverlapWarn");
    if (!box) return;
    const warns = state?.esp32_overlap_warnings || [];
    const alertItem = state?.esp32_last_overlap_alert;
    if (alertItem && alertItem.message) {
      const key = `${alertItem.cue_a || ""}|${alertItem.cue_b || ""}|${alertItem.message}`;
      if (key !== lastShownOverlapAlertKey) {
        lastShownOverlapAlertKey = key;
        try {
          alert(`⚠ ESP32 重なり警告\n\n${alertItem.message}`);
        } catch (_) {}
      }
    } else if (!alertItem) {
      lastShownOverlapAlertKey = "";
    }
    if (!warns.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    const items = warns
      .slice(0, 12)
      .map(
        (w) =>
          `<li>${w.message || `「${w.title_a}」と「${w.title_b}」が重なっています（${w.overlap_pixels}px）`}</li>`
      )
      .join("");
    box.innerHTML = `<strong>⚠ ESP32 区間重なり ${warns.length}件</strong><ul>${items}</ul>`;
  }

  function renderCues() {
    const list = $("cueList");
    const cues = state?.cues || [];
    const ov = overlappingCueIds();
    $("cueStatus").textContent = `キュー: ${cues.length}${
      ov.size ? ` ／ 重なり警告 ${state?.esp32_overlap_warnings?.length || 0}` : ""
    }`;
    const prev = new Set([...list.selectedOptions].map((o) => o.value));
    list.innerHTML = "";
    cues.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.cue_id;
      const mark = ov.has(c.cue_id) ? "⚠ " : "";
      opt.textContent = `${mark}${c.label || `${c.t.toFixed(2)}s ${c.title}`}`;
      if (prev.has(c.cue_id) || selectedCueIds.has(c.cue_id)) opt.selected = true;
      list.appendChild(opt);
    });
    updateCueHistoryUi();
    renderEsp32OverlapWarnings();
  }

  function updateCueHistoryUi() {
    const h = state?.cue_history || {};
    const undoBtn = $("btnCueUndo");
    const redoBtn = $("btnCueRedo");
    if (undoBtn) undoBtn.disabled = !h.can_undo;
    if (redoBtn) redoBtn.disabled = !h.can_redo;
    const st = $("cueHistStatus");
    if (st) {
      const i = (h.index ?? 0) + 1;
      const n = h.length ?? 1;
      st.textContent = `履歴 ${i}/${n}`;
    }
  }

  function deleteSelectedCues() {
    const list = $("cueList");
    let ids = [...list.selectedOptions].map((o) => o.value);
    if (!ids.length && selectedCueIds.size) ids = [...selectedCueIds];
    if (!ids.length) {
      alert("削除するキューを一覧から選択してください（Ctrl/Shift+クリックで複数可）");
      return;
    }
    api("/api/cues/delete", { method: "POST", body: JSON.stringify({ cue_ids: ids }) })
      .then((j) => {
        if (j.state) state = j.state;
        selectedCueIds = new Set();
        renderCues();
        drawTimeline();
      })
      .catch(alert);
  }

  function undoCues() {
    api("/api/cues/undo", { method: "POST" })
      .then((j) => {
        if (j.state) state = j.state;
        selectedCueIds = new Set();
        renderCues();
        drawTimeline();
      })
      .catch(alert);
  }

  function redoCues() {
    api("/api/cues/redo", { method: "POST" })
      .then((j) => {
        if (j.state) state = j.state;
        selectedCueIds = new Set();
        renderCues();
        drawTimeline();
      })
      .catch(alert);
  }

  function renderDmx() {
    const dmx = state?.dmx || {};
    const sel = $("comPorts");
    const cur = sel.value;
    sel.innerHTML = "";
    (dmx.ports || []).forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.label;
      sel.appendChild(o);
    });
    // 保存済みポートがリストに無い場合も選択肢として残す
    if (dmx.port && ![...sel.options].some((o) => o.value === dmx.port)) {
      const o = document.createElement("option");
      o.value = dmx.port;
      o.textContent = `${dmx.port}（保存）`;
      sel.appendChild(o);
    }
    if (dmx.port) sel.value = dmx.port;
    else if (cur) sel.value = cur;

    if ($("baseAddr") && dmx.base_addr != null && document.activeElement !== $("baseAddr")) {
      $("baseAddr").value = String(dmx.base_addr);
    }

    const connected = !!dmx.connected;
    const st = $("dmxStatus");
    st.textContent = connected ? `接続中: ${dmx.port || ""}` : "未接続";
    st.className = `badge ${connected ? "ok" : "bad"}`;
    const badge = $("dmxConnBadge");
    if (badge) {
      badge.textContent = connected ? `DMX: ${dmx.port}` : "DMX: 未接続";
      badge.className = `badge ${connected ? "ok" : "bad"}`;
    }
    const auto = !!dmx.auto_connect;
    if ($("chkDmxAuto")) $("chkDmxAuto").checked = auto;
    if ($("chkDmxAuto2")) $("chkDmxAuto2").checked = auto;

    if ($("chkDotBase") && document.activeElement !== $("chkDotBase")) {
      $("chkDotBase").checked = dmx.apply_dot_base !== false;
    }
    if ($("clubCh9")) {
      renderCh9Swatches(
        "dmxCh9Swatches",
        "clubCh9",
        "clubCh9Val",
        dmx.club_dot_ch9 ?? 12,
        (v) => {
          api("/api/dmx/ch9", {
            method: "POST",
            body: JSON.stringify({ value: Number(v) }),
          }).catch(alert);
        }
      );
    }
    if ($("syLo") && document.activeElement !== $("syLo")) {
      $("syLo").value = String(dmx.sy_lo_pct ?? 0);
      $("syLoVal").textContent = String(dmx.sy_lo_pct ?? 0);
    }
    if ($("syHi") && document.activeElement !== $("syHi")) {
      $("syHi").value = String(dmx.sy_hi_pct ?? 32);
      $("syHiVal").textContent = String(dmx.sy_hi_pct ?? 32);
    }

    const ms = $("motionSelect");
    const mi = ms.selectedIndex;
    ms.innerHTML = "";
    (dmx.motions || []).forEach((name, i) => {
      const o = document.createElement("option");
      o.value = String(i);
      o.textContent = name;
      ms.appendChild(o);
    });
    ms.selectedIndex = dmx.motion_index ?? Math.max(0, mi);

    // motion palette
    const pal = $("motionPalette");
    if (pal) {
      const cur = dmx.motion_index ?? 0;
      if (!pal.dataset.len || Number(pal.dataset.len) !== (dmx.motions || []).length) {
        pal.innerHTML = "";
        (dmx.motions || []).forEach((name, i) => {
          const el = document.createElement("div");
          el.className = "palette-item" + (i === cur ? " active" : "");
          el.textContent = name;
          el.dataset.i = String(i);
          el.addEventListener("click", () => {
            api("/api/dmx/motion/start", {
              method: "POST",
              body: JSON.stringify({
                index: i,
                speed: Number($("motionSpeed").value),
              }),
            })
              .then(refreshState)
              .catch(alert);
          });
          pal.appendChild(el);
        });
        pal.dataset.len = String((dmx.motions || []).length);
      } else {
        [...pal.children].forEach((el, i) => {
          el.classList.toggle("active", i === cur);
        });
      }
    }

    const dpal = $("dotPosPalette");
    if (dpal && !dpal.dataset.filled) {
      dpal.innerHTML = "";
      (dmx.dot_positions || []).forEach((p, i) => {
        const el = document.createElement("div");
        el.className = "palette-item";
        el.textContent = `${p.name} (CH6=${p.ch6}, CH7=${p.ch7})`;
        el.addEventListener("click", () => {
          api("/api/dmx/dot-position", {
            method: "POST",
            body: JSON.stringify({ index: i }),
          })
            .then((j) => {
              if (j.state) state = j.state;
              renderDmx();
            })
            .catch(alert);
        });
        dpal.appendChild(el);
      });
      dpal.dataset.filled = "1";
    }

    const wrap = $("chSliders");
    if (!wrap.childElementCount) {
      for (let i = 0; i < 10; i++) {
        const lab = document.createElement("label");
        lab.innerHTML = `<span class="ch-name">CH${i + 1}</span><input type="range" min="0" max="255" value="0" data-ch="${i}" orient="vertical" /><span class="ch-val">0</span>`;
        wrap.appendChild(lab);
        const inp = lab.querySelector("input");
        const sp = lab.querySelector(".ch-val") || lab.querySelector("span");
        inp.addEventListener("input", () => {
          sp.textContent = inp.value;
          pushChannels();
        });
      }
    }
    (dmx.channels || []).forEach((v, i) => {
      const inp = wrap.querySelector(`input[data-ch="${i}"]`);
      const sp = inp?.parentElement?.querySelector(".ch-val");
      if (inp && document.activeElement !== inp) {
        inp.value = String(v);
        if (sp) sp.textContent = String(v);
      }
    });
  }

  function pushChannels() {
    const ch = [...$("chSliders").querySelectorAll("input")].map((el) => Number(el.value));
    api("/api/dmx/channels", { method: "POST", body: JSON.stringify({ channels: ch }) }).catch(() => {});
  }

  function renderBle() {
    const ble = state?.ble || {};
    const connected = !!ble.connected;
    $("bleStatus").textContent = connected
      ? `接続中: ${ble.name || ble.address}`
      : "未接続";
    $("bleStatus").className = `badge ${connected ? "ok" : "bad"}`;
    const badge = $("bleConnBadge");
    if (badge) {
      badge.textContent = connected
        ? `BLE: ${ble.name || ble.address}`
        : "BLE: 未接続";
      badge.className = `badge ${connected ? "ok" : "bad"}`;
    }

    const sel = $("bleDevices");
    const cur = sel.value;
    sel.innerHTML = "";
    (ble.devices || []).forEach((d) => {
      const o = document.createElement("option");
      o.value = d.address;
      o.textContent = d.label || `${d.name} (${d.address})`;
      sel.appendChild(o);
    });
    if (cur) sel.value = cur;
    else if (ble.address && [...sel.options].some((o) => o.value === ble.address)) {
      sel.value = ble.address;
    }

    const addr = $("bleAddress");
    if (addr && document.activeElement !== addr && ble.address) {
      addr.value = ble.address;
    }
    const auto = !!ble.auto_connect;
    if ($("chkBleAuto")) $("chkBleAuto").checked = auto;
    if ($("chkBleAuto2")) $("chkBleAuto2").checked = auto;

    const modeSel = $("bleMode");
    if (modeSel && !modeSel.dataset.filled) {
      modeSel.innerHTML = "";
      (ble.modes || []).forEach((m) => {
        const o = document.createElement("option");
        o.value = String(m.id);
        o.textContent = m.label;
        modeSel.appendChild(o);
      });
      (ble.custom_fx || []).forEach((m) => {
        const o = document.createElement("option");
        o.value = String(m.id);
        o.textContent = m.label;
        modeSel.appendChild(o);
      });
      if (ble.modes?.length || ble.custom_fx?.length) modeSel.dataset.filled = "1";
    }

    const presets = $("bleColorPresets");
    if (presets && !presets.dataset.filled) {
      presets.innerHTML = "";
      (ble.color_presets || []).forEach((p) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "preset-btn";
        b.textContent = p.name;
        b.style.borderColor = `rgb(${p.r},${p.g},${p.b})`;
        b.addEventListener("click", () => {
          if (blePalette) blePalette.setRgb(p.r, p.g, p.b);
          else {
            $("ledR").value = p.r;
            $("ledG").value = p.g;
            $("ledB").value = p.b;
          }
          updateSwatch();
          api("/api/ble/color", {
            method: "POST",
            body: JSON.stringify({
              r: p.r,
              g: p.g,
              b: p.b,
              brightness: Number($("ledBright").value) / 100,
            }),
          }).catch(alert);
        });
        presets.appendChild(b);
      });
      presets.dataset.filled = "1";
    }
    updateSwatch();
  }

  function fillEsp32ColorPresets(list) {
    const presets = $("esp32ColorPresets");
    if (!presets) return;
    const rows = list && list.length ? list : ESP32_COLOR_FALLBACK;
    const sig = rows.map((p) => p.name || `${p.r},${p.g},${p.b}`).join("|");
    if (presets.dataset.sig === sig && presets.childElementCount) return;
    presets.innerHTML = "";
    rows.forEach((p) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "preset-btn";
      b.textContent = p.name;
      b.style.borderColor = `rgb(${p.r},${p.g},${p.b})`;
      b.addEventListener("click", () => {
        if (esp32Palette) esp32Palette.setRgb(p.r, p.g, p.b);
        else {
          $("esp32R").value = p.r;
          $("esp32G").value = p.g;
          $("esp32B").value = p.b;
        }
        updateEsp32Swatch();
        ensureEsp32Ready({ enable: true })
          .then(() =>
            api("/api/esp32/color", {
              method: "POST",
              body: JSON.stringify({
                r: p.r,
                g: p.g,
                b: p.b,
                brightness: Number($("esp32Bright").value) / 100,
              }),
            })
          )
          .then(applyEsp32State)
          .catch(alert);
      });
      presets.appendChild(b);
    });
    presets.dataset.sig = sig;
  }

  function fillEsp32PatternPresets(list, activeId) {
    const patBox = $("esp32PatternPresets");
    if (!patBox) return;
    const rows = list && list.length ? list : ESP32_PATTERN_FALLBACK;
    const sig = rows.map((p) => p.id).join("|");
    if (patBox.dataset.sig !== sig || !patBox.childElementCount) {
      patBox.innerHTML = "";
      rows.forEach((p) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "preset-btn";
        b.dataset.pat = p.id;
        b.title = p.desc || p.id;
        b.textContent = p.label || p.id;
        b.addEventListener("click", () => startEsp32Pattern(p.id));
        patBox.appendChild(b);
      });
      patBox.dataset.sig = sig;
    }
    [...patBox.querySelectorAll("button")].forEach((btn) => {
      btn.classList.toggle("active", !!activeId && btn.dataset.pat === activeId);
    });
  }

  function renderEsp32() {
    const esp = state?.esp32 || {};
    const enabled = !!esp.enabled;
    const statusEl = $("esp32Status");
    if (statusEl) {
      statusEl.textContent = esp.status || (enabled ? "有効" : "無効");
      statusEl.className = `badge ${enabled ? "ok" : "bad"}`;
    }
    const host = $("esp32Host");
    // サーバ側ホストが空のときは入力中の値を消さない
    if (host && document.activeElement !== host && esp.host) {
      host.value = esp.host;
    }
    const uni = $("esp32Universe");
    if (uni && document.activeElement !== uni && esp.universe != null) {
      uni.value = String(esp.universe);
    }
    const px = $("esp32Pixels");
    if (px && document.activeElement !== px && esp.pixel_count != null) {
      px.value = String(esp.pixel_count);
    }
    if ($("chkEsp32Auto") && document.activeElement !== $("chkEsp32Auto")) {
      $("chkEsp32Auto").checked = !!esp.auto_connect;
    }
    if ($("esp32PatSpeed") && document.activeElement !== $("esp32PatSpeed")) {
      const sp = Math.round((esp.pattern_speed || 1) * 100);
      $("esp32PatSpeed").value = String(Math.max(5, Math.min(300, sp)));
      if ($("esp32PatSpeedVal")) $("esp32PatSpeedVal").textContent = `${(sp / 100).toFixed(2)}×`;
    }
    fillEsp32ColorPresets(state?.ble?.color_presets || ESP32_COLOR_FALLBACK);
    fillEsp32PatternPresets(
      esp.patterns && esp.patterns.length ? esp.patterns : ESP32_PATTERN_FALLBACK,
      esp.pattern_running ? esp.pattern_id : ""
    );
    const segCountEl = $("esp32SegCount");
    if (segCountEl && document.activeElement !== segCountEl && esp.segment_count != null) {
      segCountEl.value = String(esp.segment_count);
    }
    renderEsp32Segments(esp);
    updateEsp32Swatch();
  }

  function esp32SegmentRanges(pixelCount, segmentCount) {
    const nPix = Math.max(1, Number(pixelCount) || 1);
    const nSeg = Math.max(1, Number(segmentCount) || 1);
    const base = Math.floor(nPix / nSeg);
    const rem = nPix % nSeg;
    const ranges = [];
    let start = 0;
    for (let i = 0; i < nSeg; i++) {
      const len = base + (i < rem ? 1 : 0);
      ranges.push([start, start + len]);
      start += len;
    }
    return ranges;
  }

  function syncEsp32SelectionFromState(esp) {
    if (Array.isArray(esp?.selected)) {
      esp32Selected = esp.selected.map((n) => Number(n)).filter((n) => Number.isFinite(n));
    }
  }

  function pushEsp32Selection() {
    api("/api/esp32/selection", {
      method: "POST",
      body: JSON.stringify({ indices: esp32Selected }),
    })
      .then((j) => {
        if (j.state) mergeState(j.state);
      })
      .catch(() => {});
  }

  function renderEsp32Segments(esp) {
    const strip = $("esp32SegmentStrip");
    if (!strip) return;
    syncEsp32SelectionFromState(esp);
    const count = Math.max(1, Number(esp?.segment_count) || Number($("esp32SegCount")?.value) || 8);
    const segs = Array.isArray(esp?.segments) ? esp.segments : [];
    const pixels = Number(esp?.pixel_count) || Number($("esp32Pixels")?.value) || 720;
    const ranges = esp32SegmentRanges(pixels, count);
    const selSet = new Set(esp32Selected);
    const sig = `${count}|${pixels}|${[...selSet].join(",")}|${segs
      .map((s) => `${s.on ? 1 : 0}:${s.r},${s.g},${s.b}`)
      .join(";")}`;
    if (strip.dataset.sig !== sig) {
      strip.innerHTML = "";
      strip.style.gridTemplateColumns = `repeat(${Math.min(count, 16)}, minmax(44px, 1fr))`;
      for (let i = 0; i < count; i++) {
        const seg = segs[i] || { r: 0, g: 0, b: 0, on: false };
        const [a, b] = ranges[i] || [0, 0];
        const cell = document.createElement("button");
        cell.type = "button";
        const selected = selSet.has(i);
        cell.className = `esp32-seg-cell${seg.on ? " on" : ""}${selected ? " selected" : ""}`;
        cell.dataset.index = String(i);
        const bg = seg.on ? `rgb(${seg.r},${seg.g},${seg.b})` : "#1a2030";
        cell.style.background = bg;
        const lum = 0.299 * (seg.r || 0) + 0.587 * (seg.g || 0) + 0.114 * (seg.b || 0);
        cell.style.color = seg.on && lum > 140 ? "#111" : "#d7dde6";
        cell.innerHTML = `<span class="seg-idx">#${i + 1}</span><span class="seg-px">${a + 1}-${b}</span>`;
        cell.title = `区間 ${i + 1}（pixel ${a + 1}–${b}）\nクリック: 選択 / Ctrl: 複数 / Shift: 範囲`;
        cell.addEventListener("click", (ev) => onEsp32SegmentClick(i, ev, count));
        strip.appendChild(cell);
      }
      strip.dataset.sig = sig;
    }
    const info = $("esp32SegInfo");
    if (info) {
      const lit = segs.filter((s) => s.on).length;
      const per = Math.floor(pixels / count);
      info.textContent = `${count}分割 ≈${per}px / 選択 ${esp32Selected.length} / 点灯 ${lit}`;
    }
    renderEsp32Groups(esp);
  }

  function onEsp32SegmentClick(index, ev, count) {
    const ctrl = !!ev?.ctrlKey || !!ev?.metaKey;
    const shift = !!ev?.shiftKey;
    if (shift && esp32SelAnchor != null) {
      const a = Math.min(esp32SelAnchor, index);
      const b = Math.max(esp32SelAnchor, index);
      const range = [];
      for (let i = a; i <= b; i++) range.push(i);
      if (ctrl) {
        const set = new Set(esp32Selected);
        range.forEach((i) => set.add(i));
        esp32Selected = [...set].sort((x, y) => x - y);
      } else {
        esp32Selected = range;
      }
    } else if (ctrl) {
      const set = new Set(esp32Selected);
      if (set.has(index)) set.delete(index);
      else set.add(index);
      esp32Selected = [...set].sort((x, y) => x - y);
      esp32SelAnchor = index;
    } else {
      esp32Selected = [index];
      esp32SelAnchor = index;
    }
    esp32ActiveGroupSpans = null;
    // 即 UI 反映
    if (state?.esp32) state.esp32.selected = [...esp32Selected];
    renderEsp32Segments(state?.esp32 || { segment_count: count });
    pushEsp32Selection();
    // 選択した瞬間に現在色をライブ適用
    livePaintEsp32Selection();
  }

  function livePaintEsp32Selection(optRanges, optSpans) {
    const r = Number($("esp32R")?.value || 255);
    const g = Number($("esp32G")?.value || 80);
    const b = Number($("esp32B")?.value || 40);
    const brightness = Number($("esp32Bright")?.value || 100) / 100;
    const segCount = Number(state?.esp32?.segment_count || $("esp32SegCount")?.value || 8);
    const spans =
      optSpans && optSpans.length
        ? optSpans
        : esp32ActiveGroupSpans && esp32ActiveGroupSpans.length
          ? esp32ActiveGroupSpans
          : null;
    const ranges = spans
      ? null
      : optRanges && optRanges.length
        ? optRanges
        : esp32Selected.length
          ? indicesToFracRanges(esp32Selected, segCount)
          : null;
    if ((!spans || !spans.length) && (!ranges || !ranges.length)) return;
    if (esp32PaintTimer) clearTimeout(esp32PaintTimer);
    esp32PaintTimer = setTimeout(() => {
      const body = {
        r,
        g,
        b,
        on: true,
        brightness,
        others_off: false,
      };
      if (spans && spans.length) {
        body.spans = spans;
      } else {
        body.ranges = ranges;
        body.indices = esp32Selected;
        body.segment_count = segCount;
      }
      ensureEsp32Ready({ enable: true })
        .then(() =>
          api("/api/esp32/paint", {
            method: "POST",
            body: JSON.stringify(body),
          })
        )
        .then((j) => {
          if (j?.state) mergeState(j.state);
          // 選択はクライアント優先で維持
          if (state?.esp32) state.esp32.selected = [...esp32Selected];
          renderEsp32Segments(state?.esp32 || {});
        })
        .catch(() => {});
    }, 40);
  }

  function renderEsp32Groups(esp) {
    const box = $("esp32GroupList");
    if (!box) return;
    const groups = esp?.groups || [];
    const sig =
      `n=${esp?.segment_count || "?"}|px=${esp?.pixel_count || "?"}|` +
      groups
        .map(
          (g) =>
            `${g.id}:${g.pixel_total || 0}:${JSON.stringify(g.spans || [])}`
        )
        .join("|");
    if (box.dataset.sig === sig) return;
    box.innerHTML = "";
    groups.forEach((g) => {
      const spans = g.spans || [];
      const overlap = g.overlap_indices || [];
      const px = g.pixel_total != null ? g.pixel_total : spans.reduce((n, s) => n + (Number(s[1]) - Number(s[0])), 0);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "preset-btn esp32-group-btn";
      b.textContent = `${g.name} [${px}px]`;
      b.title = `ピクセル固定 ${px}px\n${spans
        .map((s) => `${Number(s[0]) + 1}–${s[1]}`)
        .join(", ")}\n（分割変換なし）`;
      b.addEventListener("click", () => {
        esp32ActiveGroupSpans = spans.map((s) => [Number(s[0]), Number(s[1])]);
        // 表示用ハイライトのみ（グループ定義の再マップではない）
        esp32Selected = [...overlap].map(Number).sort((a, c) => a - c);
        esp32SelAnchor = esp32Selected[0] ?? null;
        if (state?.esp32) state.esp32.selected = [...esp32Selected];
        $("esp32GroupName").value = g.name || "";
        $("esp32GroupName").dataset.gid = g.id;
        renderEsp32Segments(state?.esp32 || {});
        pushEsp32Selection();
        livePaintEsp32Selection(null, esp32ActiveGroupSpans);
        [...box.querySelectorAll(".esp32-group-btn")].forEach((el) =>
          el.classList.toggle("active", el === b)
        );
      });
      box.appendChild(b);
    });
    box.dataset.sig = sig;
  }

  function esp32FormConfig() {
    return {
      host: ($("esp32Host")?.value || "").trim(),
      universe: Number($("esp32Universe")?.value) || 0,
      pixel_count: Number($("esp32Pixels")?.value) || 720,
      auto_connect: !!$("chkEsp32Auto")?.checked,
    };
  }

  async function ensureEsp32Ready({ enable = true } = {}) {
    const cfg = esp32FormConfig();
    if (!cfg.host) {
      throw new Error("先にホスト IP（例: 192.168.2.107）を入力してください");
    }
    const j = await api("/api/esp32/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    });
    if (j?.state) mergeState(j.state);
    if (enable && !state?.esp32?.enabled) {
      const j2 = await api("/api/esp32/enable", {
        method: "POST",
        body: JSON.stringify({ enabled: true }),
      });
      if (j2?.state) mergeState(j2.state);
    }
    renderEsp32();
  }

  function startEsp32Pattern(patternId) {
    const segCount = Number(state?.esp32?.segment_count || $("esp32SegCount")?.value || 8);
    const ranges = esp32Selected.length
      ? indicesToFracRanges(esp32Selected, segCount)
      : null;
    ensureEsp32Ready({ enable: true })
      .then(() =>
        api("/api/esp32/pattern", {
          method: "POST",
          body: JSON.stringify({
            pattern_id: patternId,
            speed: Number($("esp32PatSpeed")?.value || 100) / 100,
            brightness: Number($("esp32Bright").value) / 100,
            r: Number($("esp32R").value),
            g: Number($("esp32G").value),
            b: Number($("esp32B").value),
            ranges,
            segment_indices: esp32Selected.length ? [...esp32Selected] : null,
            others_off: true,
          }),
        })
      )
      .then(applyEsp32State)
      .catch(alert);
  }

  function updateEsp32Swatch() {
    const r = $("esp32R")?.value ?? 255;
    const g = $("esp32G")?.value ?? 80;
    const b = $("esp32B")?.value ?? 40;
    if ($("esp32Swatch")) $("esp32Swatch").style.background = `rgb(${r},${g},${b})`;
    if ($("esp32RgbLabel")) $("esp32RgbLabel").textContent = `RGB ${r}, ${g}, ${b}`;
  }

  function renderAi() {
    const ai = state?.ai || {};
    $("aiStatus").textContent = ai.status || (ai.active ? "動作中" : "停止中");
    if (ai.sensitivity != null && document.activeElement !== $("aiSens")) {
      $("aiSens").value = String(ai.sensitivity);
      $("aiSensVal").textContent = Number(ai.sensitivity).toFixed(1);
    }

    const dev = $("aiDevice");
    if (dev) {
      const prev = dev.value;
      dev.innerHTML = "";
      (ai.devices || []).forEach((d) => {
        const o = document.createElement("option");
        o.value = d.id == null ? "default" : String(d.id);
        o.textContent = d.label;
        o.dataset.label = d.label;
        dev.appendChild(o);
      });
      if (ai.device_id != null && [...dev.options].some((o) => o.value === String(ai.device_id))) {
        dev.value = String(ai.device_id);
      } else if (ai.device_label) {
        const match = [...dev.options].find((o) => o.dataset.label === ai.device_label);
        if (match) dev.value = match.value;
        else if (prev) dev.value = prev;
      }
    }

    const mode = $("aiMode");
    if (mode) {
      const cur = mode.value || ai.mode;
      mode.innerHTML = "";
      (ai.modes || ["バランス", "パーティー", "シネマ", "チル"]).forEach((m) => {
        const o = document.createElement("option");
        o.value = m;
        o.textContent = m;
        mode.appendChild(o);
      });
      if (cur) mode.value = cur;
      else if (ai.mode) mode.value = ai.mode;
    }
  }

  function fillTeMotionOptions() {
    const sel = $("teMotion");
    if (!sel) return;
    const motions = state?.dmx?.motions || [];
    if (sel.dataset.len === String(motions.length)) return;
    sel.innerHTML = "";
    motions.forEach((name, i) => {
      const o = document.createElement("option");
      o.value = String(i);
      o.textContent = name;
      sel.appendChild(o);
    });
    sel.dataset.len = String(motions.length);
  }

  function fillTeLedModes() {
    const sel = $("teLedMode");
    if (!sel) return;
    // 毎回埋め直して最新を反映
    const prev = sel.value;
    sel.innerHTML = "";
    const add = (id, label) => {
      const o = document.createElement("option");
      o.value = id == null ? "" : String(id);
      o.textContent = label;
      sel.appendChild(o);
    };
    add(null, "固定色（RGB）");
    (state?.ble?.custom_fx || []).forEach((m) => add(m.id, m.label));
    (state?.ble?.modes || []).forEach((m) => add(m.id, m.label));
    if (prev !== undefined) sel.value = prev;
  }

  function fillTeColorPresets() {
    const wrap = $("teColorPresets");
    if (!wrap || wrap.dataset.filled) return;
    wrap.innerHTML = "";
    (state?.ble?.color_presets || []).forEach((p) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "preset-btn";
      b.textContent = p.name;
      b.style.borderColor = `rgb(${p.r},${p.g},${p.b})`;
      b.addEventListener("click", () => {
        if (tePalette) tePalette.setRgb(p.r, p.g, p.b);
        else {
          $("teLedR").value = p.r;
          $("teLedG").value = p.g;
          $("teLedB").value = p.b;
        }
      });
      wrap.appendChild(b);
    });
    wrap.dataset.filled = "1";
  }

  function syncDevicePanels() {
    const laserOn = !!$("teLaser")?.checked;
    const ledOn = !!$("teLed")?.checked;
    const espOn = !!$("teEsp32")?.checked;
    if ($("teLaserPanel")) $("teLaserPanel").hidden = !laserOn;
    if ($("teLedPanel")) $("teLedPanel").hidden = !(ledOn || espOn);
    if ($("teEsp32Panel")) $("teEsp32Panel").hidden = !espOn;
    if (espOn) {
      fillTeEsp32Options();
      syncTeEsp32TargetUi();
    }
  }

  function fillTeEsp32Options() {
    const gsel = $("teEsp32Group");
    if (gsel) {
      const prev = gsel.value;
      gsel.innerHTML = "";
      const opt0 = document.createElement("option");
      opt0.value = "";
      opt0.textContent = "（グループを選択）";
      gsel.appendChild(opt0);
      (state?.esp32?.groups || []).forEach((g) => {
        const o = document.createElement("option");
        o.value = g.id;
        const px = g.pixel_total != null ? g.pixel_total : "?";
        o.textContent = `${g.name} [${px}px]`;
        gsel.appendChild(o);
      });
      if (prev) gsel.value = prev;
    }
    const psel = $("teEsp32Pattern");
    if (psel) {
      const list =
        state?.esp32?.patterns?.length ? state.esp32.patterns : ESP32_PATTERN_FALLBACK;
      const sig = list.map((p) => p.id).join("|");
      if (psel.dataset.sig !== sig) {
        const prev = psel.value;
        psel.innerHTML = "";
        list.forEach((p) => {
          const o = document.createElement("option");
          o.value = p.id;
          o.textContent = p.label || p.id;
          psel.appendChild(o);
        });
        if (prev) psel.value = prev;
        psel.dataset.sig = sig;
      }
    }
  }

  function syncTeEsp32TargetUi() {
    const target = $("teEsp32Target")?.value || "all";
    const fx = $("teEsp32Fx")?.value || "color";
    if ($("teEsp32Group")) $("teEsp32Group").disabled = target !== "group";
    if ($("btnTeEsp32UseSelection")) $("btnTeEsp32UseSelection").disabled = target !== "segments";
    if ($("teEsp32Pattern")) $("teEsp32Pattern").disabled = fx !== "pattern";
    if ($("teEsp32PatSpeed")) $("teEsp32PatSpeed").disabled = fx !== "pattern";
    renderTeEsp32SegChips();
  }

  function parseTeEsp32Segments() {
    const raw = ($("teEsp32Segments")?.value || "").trim();
    if (!raw) return [];
    return raw
      .split(",")
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n) && n >= 0);
  }

  function parseTeEsp32Ranges() {
    const raw = ($("teEsp32Ranges")?.value || "").trim();
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter((rr) => Array.isArray(rr) && rr.length >= 2)
        .map((rr) => [Number(rr[0]), Number(rr[1])])
        .filter((rr) => Number.isFinite(rr[0]) && Number.isFinite(rr[1]));
    } catch (_) {
      return [];
    }
  }

  function indicesToFracRanges(indices, segmentCount) {
    const n = Math.max(1, Number(segmentCount) || 1);
    const clean = [...new Set(indices.map(Number).filter((i) => Number.isFinite(i)))]
      .map((i) => Math.max(0, Math.min(n - 1, i)))
      .sort((a, b) => a - b);
    if (!clean.length) return [];
    const ranges = [];
    let start = clean[0];
    let prev = clean[0];
    for (let k = 1; k < clean.length; k++) {
      const i = clean[k];
      if (i === prev + 1) {
        prev = i;
        continue;
      }
      ranges.push([start / n, (prev + 1) / n]);
      start = prev = i;
    }
    ranges.push([start / n, (prev + 1) / n]);
    return ranges;
  }

  function renderTeEsp32SegChips() {
    const box = $("teEsp32SegChips");
    if (!box) return;
    const idxs = parseTeEsp32Segments();
    box.innerHTML = "";
    if (!idxs.length) {
      box.innerHTML = `<span class="hint">未設定（「現在の選択を取り込み」）</span>`;
      return;
    }
    idxs.forEach((i) => {
      const span = document.createElement("span");
      span.className = "esp32-chip";
      span.textContent = `#${i + 1}`;
      box.appendChild(span);
    });
  }

  function defaultNewTileForm() {
    const dmx = state?.dmx || {};
    editChannels = [...(dmx.channels || [100, 128, 0, 0, 0, 64, 16, 63, 12, 0])];
    while (editChannels.length < 10) editChannels.push(0);
    $("teTitle").value = `スロット ${(state?.tiles?.length || 0) + 1}`;
    $("teHotkey").value = "";
    $("teLaser").checked = false;
    $("teLed").checked = false;
    if ($("teEsp32")) $("teEsp32").checked = false;
    $("teRunMotion").checked = true;
    $("teDotBase").checked = dmx.apply_dot_base !== false;
    fillTeMotionOptions();
    $("teMotion").value = String(dmx.motion_index ?? 0);
    $("teSpeed").value = String(dmx.motion_speed ?? 1.35);
    $("teSpeedVal").textContent = Number(dmx.motion_speed ?? 1.35).toFixed(2);
    $("teCh9").value = String(dmx.club_dot_ch9 ?? editChannels[8] ?? 12);
    renderCh9Swatches("teCh9Swatches", "teCh9", "teCh9Val", $("teCh9").value);
    if (tePalette) tePalette.setRgb(255, 80, 40);
    else {
      $("teLedR").value = "255";
      $("teLedG").value = "80";
      $("teLedB").value = "40";
    }
    $("teLedBright").value = "100";
    $("teLedBrightVal").textContent = "100";
    fillTeLedModes();
    $("teLedMode").value = "";
    $("teLedSpeed").value = "50";
    $("teLedSpeedVal").textContent = "50";
    if ($("teEsp32Target")) $("teEsp32Target").value = "all";
    if ($("teEsp32Group")) $("teEsp32Group").value = "";
    if ($("teEsp32Fx")) $("teEsp32Fx").value = "color";
    if ($("teEsp32Pattern")) $("teEsp32Pattern").value = ESP32_PATTERN_FALLBACK[0]?.id || "";
    if ($("teEsp32PatSpeed")) {
      $("teEsp32PatSpeed").value = "100";
      if ($("teEsp32PatSpeedVal")) $("teEsp32PatSpeedVal").textContent = "1.00×";
    }
    if ($("teEsp32OthersOff")) $("teEsp32OthersOff").checked = true;
    if ($("teEsp32Segments")) $("teEsp32Segments").value = "";
    if ($("teEsp32SegCount")) $("teEsp32SegCount").value = "";
    if ($("teEsp32Ranges")) $("teEsp32Ranges").value = "[]";
    syncDevicePanels();
  }

  function loadTileIntoForm(idx) {
    const tiles = state?.tiles || [];
    if (!tiles.length) {
      $("tileEditStatus").textContent = "タイルなし";
      renderTileSummary(null);
      return;
    }
    editTileIndex = Math.max(0, Math.min(tiles.length - 1, idx));
    const t = tiles[editTileIndex];
    editChannels = [...(t.channels || editChannels)];
    while (editChannels.length < 10) editChannels.push(0);
    $("teTitle").value = t.title || "";
    $("teHotkey").value = t.hotkey || "";
    $("teLaser").checked = t.apply_laser !== false;
    $("teLed").checked = !!t.apply_led;
    if ($("teEsp32")) $("teEsp32").checked = !!t.apply_esp32;
    $("teRunMotion").checked = t.run_motion !== false;
    $("teDotBase").checked = t.apply_dot_base !== false;
    fillTeMotionOptions();
    $("teMotion").value = String(t.motion_index || 0);
    $("teSpeed").value = String(t.motion_speed ?? 1.35);
    $("teSpeedVal").textContent = Number(t.motion_speed ?? 1.35).toFixed(2);
    $("teCh9").value = String(t.club_dot_ch9 ?? 12);
    renderCh9Swatches("teCh9Swatches", "teCh9", "teCh9Val", $("teCh9").value);
    if (tePalette) tePalette.setRgb(t.led_r ?? 255, t.led_g ?? 80, t.led_b ?? 40);
    else {
      $("teLedR").value = String(t.led_r ?? 255);
      $("teLedG").value = String(t.led_g ?? 80);
      $("teLedB").value = String(t.led_b ?? 40);
    }
    $("teLedBright").value = String(t.led_brightness ?? 100);
    $("teLedBrightVal").textContent = String(t.led_brightness ?? 100);
    fillTeLedModes();
    $("teLedMode").value = t.led_mode == null ? "" : String(t.led_mode);
    $("teLedSpeed").value = String(t.led_speed ?? 50);
    $("teLedSpeedVal").textContent = String(t.led_speed ?? 50);
    if ($("teEsp32Target")) $("teEsp32Target").value = t.esp32_target || "all";
    if ($("teEsp32Group")) $("teEsp32Group").value = t.esp32_group_id || "";
    if ($("teEsp32Fx")) $("teEsp32Fx").value = t.esp32_fx || "color";
    if ($("teEsp32Pattern")) $("teEsp32Pattern").value = t.esp32_pattern_id || "";
    if ($("teEsp32PatSpeed")) {
      const sp = Math.round((t.esp32_pattern_speed || 1) * 100);
      $("teEsp32PatSpeed").value = String(sp);
      if ($("teEsp32PatSpeedVal")) $("teEsp32PatSpeedVal").textContent = `${(sp / 100).toFixed(2)}×`;
    }
    if ($("teEsp32OthersOff")) $("teEsp32OthersOff").checked = t.esp32_others_off !== false;
    if ($("teEsp32Segments")) {
      $("teEsp32Segments").value = (t.esp32_segments || []).join(",");
    }
    if ($("teEsp32SegCount")) {
      $("teEsp32SegCount").value =
        t.esp32_segment_count != null ? String(t.esp32_segment_count) : "";
    }
    if ($("teEsp32Ranges")) {
      $("teEsp32Ranges").value = JSON.stringify(t.esp32_ranges || []);
    }
    $("tileEditStatus").textContent = `#${editTileIndex + 1} / ${tiles.length}`;
    const list = $("tileEditList");
    if (list) list.selectedIndex = editTileIndex;
    syncDevicePanels();
    renderTileSummary(t);
  }

  function readTileFromForm() {
    const modeVal = $("teLedMode").value;
    const laserOn = $("teLaser").checked;
    const ledOn = $("teLed").checked;
    const espOn = !!$("teEsp32")?.checked;
    const ch = [...editChannels];
    ch[8] = Number($("teCh9").value);
    return {
      title: $("teTitle").value.trim() || "タイル",
      channels: ch,
      motion_index: Number($("teMotion").value) || 0,
      motion_speed: Number($("teSpeed").value),
      apply_dot_base: $("teDotBase").checked,
      club_dot_ch9: Number($("teCh9").value),
      run_motion: laserOn && $("teRunMotion").checked,
      hotkey: $("teHotkey").value.trim() || null,
      apply_laser: laserOn,
      apply_led: ledOn,
      apply_esp32: espOn,
      led_r: Number($("teLedR").value),
      led_g: Number($("teLedG").value),
      led_b: Number($("teLedB").value),
      led_brightness: Number($("teLedBright").value),
      led_mode: modeVal === "" ? null : Number(modeVal),
      led_speed: Number($("teLedSpeed").value),
      esp32_target: $("teEsp32Target")?.value || "all",
      esp32_segments: parseTeEsp32Segments(),
      esp32_segment_count: (() => {
        const v = Number($("teEsp32SegCount")?.value);
        return Number.isFinite(v) && v > 0 ? v : null;
      })(),
      esp32_ranges: parseTeEsp32Ranges(),
      esp32_group_id: $("teEsp32Group")?.value || null,
      esp32_fx: $("teEsp32Fx")?.value || "color",
      esp32_pattern_id: $("teEsp32Pattern")?.value || null,
      esp32_pattern_speed: Number($("teEsp32PatSpeed")?.value || 100) / 100,
      esp32_others_off: !!$("teEsp32OthersOff")?.checked,
    };
  }

  function renderTileSummary(t) {
    const box = $("tileSummary");
    if (!box) return;
    if (!t) {
      box.innerHTML = `<h3>選択中</h3><p class="hint">一覧から選ぶか「新規タイル」で作成してください。</p>`;
      return;
    }
    const devices = [];
    if (t.apply_laser !== false) devices.push("レーザー");
    if (t.apply_led) devices.push("BLE");
    if (t.apply_esp32) devices.push("ESP32");
    const motionName = (state?.dmx?.motions || [])[t.motion_index || 0] || `idx ${t.motion_index || 0}`;
    let ledInfo = "—";
    if (t.apply_led) {
      if (t.led_mode == null) {
        ledInfo = `RGB(${t.led_r},${t.led_g},${t.led_b}) 明るさ${t.led_brightness}`;
      } else {
        const modes = [...(state?.ble?.custom_fx || []), ...(state?.ble?.modes || [])];
        const m = modes.find((x) => Number(x.id) === Number(t.led_mode));
        ledInfo = m ? m.label : `mode ${t.led_mode}`;
      }
    }
    let espInfo = "—";
    if (t.apply_esp32) {
      const tgt =
        t.esp32_target === "group"
          ? `グループ:${t.esp32_group_id || "?"}`
          : t.esp32_target === "segments"
            ? `区間[${(t.esp32_segments || []).map((i) => Number(i) + 1).join(",")}]` +
              (t.esp32_segment_count ? `(保存${t.esp32_segment_count}分割)` : "")
            : "全体";
      const fx =
        t.esp32_fx === "pattern"
          ? `パターン:${t.esp32_pattern_id || "?"}`
          : `単色 RGB(${t.led_r},${t.led_g},${t.led_b})`;
      espInfo = `${tgt} / ${fx}`;
    }
    box.innerHTML = `
      <h3>${t.title || "タイル"}</h3>
      <dl>
        <dt>機器</dt><dd>${devices.length ? devices.join(" + ") : "なし"}</dd>
        <dt>ホットキー</dt><dd>${t.hotkey || "—"}</dd>
        <dt>モーション</dt><dd>${t.apply_laser !== false ? motionName : "—"}</dd>
        <dt>LED</dt><dd>${ledInfo}</dd>
        <dt>ESP32</dt><dd>${espInfo}</dd>
      </dl>
      <p class="hint">「編集…」または一覧をダブルクリックでサイドバーを開きます。</p>`;
  }

  function openTileSidebar(mode, idx) {
    sidebarMode = mode;
    fillTeMotionOptions();
    fillTeLedModes();
    fillTeColorPresets();
    if (mode === "new") {
      $("tileSidebarTitle").textContent = "新規タイル";
      $("btnTileSidebarSave").textContent = "作成";
      defaultNewTileForm();
    } else {
      $("tileSidebarTitle").textContent = "タイル編集";
      $("btnTileSidebarSave").textContent = "保存";
      loadTileIntoForm(idx ?? editTileIndex);
    }
    syncDevicePanels();
    $("tileSidebar").hidden = false;
    $("tileSidebar").setAttribute("aria-hidden", "false");
    $("tileSidebarBackdrop").hidden = false;
    $("teTitle").focus();
  }

  function closeTileSidebar() {
    $("tileSidebar").hidden = true;
    $("tileSidebar").setAttribute("aria-hidden", "true");
    $("tileSidebarBackdrop").hidden = true;
    api("/api/tiles/release", {
      method: "POST",
      body: JSON.stringify({ source: "preview" }),
    }).catch(() => {});
  }

  function renderTileEditor() {
    if (!$("tileEditList")) return;
    fillTeMotionOptions();
    fillTeLedModes();
    fillTeColorPresets();
    const list = $("tileEditList");
    const tiles = state?.tiles || [];
    const prev = list.selectedIndex;
    list.innerHTML = "";
    tiles.forEach((t, i) => {
      const o = document.createElement("option");
      o.value = String(i);
      const tags = [];
      if (t.apply_laser !== false) tags.push("L");
      if (t.apply_led) tags.push("LED");
      const hk = t.hotkey ? ` [${t.hotkey}]` : "";
      o.textContent = `${i + 1}. ${tags.length ? "[" + tags.join("+") + "] " : ""}${t.title || "?"}${hk}`;
      list.appendChild(o);
    });
    if (tiles.length) {
      const idx = prev >= 0 && prev < tiles.length ? prev : editTileIndex;
      list.selectedIndex = idx;
      editTileIndex = idx;
      $("tileEditStatus").textContent = `#${idx + 1} / ${tiles.length}`;
      renderTileSummary(tiles[idx]);
    } else {
      $("tileEditStatus").textContent = "タイルなし";
      renderTileSummary(null);
    }
  }

  function updateSwatch() {
    const r = $("ledR")?.value ?? 255;
    const g = $("ledG")?.value ?? 80;
    const b = $("ledB")?.value ?? 40;
    if ($("ledSwatch")) $("ledSwatch").style.background = `rgb(${r},${g},${b})`;
    if ($("bleLedRgbLabel")) $("bleLedRgbLabel").textContent = `RGB ${r}, ${g}, ${b}`;
  }

  // ---- timeline canvas ----
  function visibleDur() {
    const dur = state?.timeline?.duration || 0;
    if (dur <= 0) return 1;
    return Math.max(0.05, dur / Math.max(1, zoom));
  }

  function xToTime(x, w) {
    const dur = state?.timeline?.duration || 0;
    const vis = visibleDur();
    const t = viewStart + (x / Math.max(1, w)) * vis;
    return Math.max(0, Math.min(dur, t));
  }

  function timeToX(t, w) {
    const vis = visibleDur();
    return ((t - viewStart) / vis) * w;
  }

  function drawTimeline() {
    const cv = $("tlCanvas");
    const ctx = cv.getContext("2d");
    const w = cv.clientWidth || 800;
    const h = 160;
    if (cv.width !== w) cv.width = w;
    cv.height = h;
    ctx.fillStyle = "#12151a";
    ctx.fillRect(0, 0, w, h);
    const rh = 22;
    const wh = 72;
    const ch = h - rh - wh;
    ctx.fillStyle = "#242830";
    ctx.fillRect(0, 0, w, rh);
    ctx.fillStyle = "#0e1116";
    ctx.fillRect(0, rh, w, wh);
    ctx.fillStyle = "#1a2030";
    ctx.fillRect(0, rh + wh, w, ch);

    const dur = state?.timeline?.duration || 0;
    const peaks = state?.timeline?.peaks;
    const vis = visibleDur();
    const vis0 = viewStart;
    const vis1 = vis0 + vis;

    if (dur <= 0) {
      ctx.fillStyle = "#5a6270";
      ctx.font = "14px Segoe UI";
      ctx.fillText("音楽ファイルを開くと波形が表示されます", w / 2 - 140, rh + wh / 2);
    } else {
      // ruler
      let step = vis <= 8 ? 1 : vis <= 30 ? 5 : vis <= 120 ? 10 : 30;
      ctx.fillStyle = "#9aa0a6";
      ctx.font = "11px Consolas";
      for (let t = Math.floor(vis0 / step) * step; t <= vis1 + 1e-6; t += step) {
        const x = timeToX(t, w);
        ctx.strokeStyle = "#6a7380";
        ctx.beginPath();
        ctx.moveTo(x, rh - 8);
        ctx.lineTo(x, rh);
        ctx.stroke();
        const m = Math.floor(t / 60);
        const s = (t - m * 60).toFixed(2);
        ctx.fillText(`${m}:${s.padStart(5, "0")}`, x + 2, 12);
      }
      // wave
      if (peaks && peaks.length) {
        const mid = rh + wh / 2;
        for (let xi = 0; xi < w; xi++) {
          const t = vis0 + ((xi + 0.5) / w) * vis;
          const pi = Math.max(0, Math.min(peaks.length - 1, Math.floor((t / dur) * peaks.length)));
          const amp = Math.max(1, peaks[pi] * (wh * 0.45));
          ctx.fillStyle = "#3d8bfd";
          ctx.fillRect(xi, mid - amp, 1, amp * 2);
        }
      }
      // clips
      const ovIds = overlappingCueIds();
      (state?.cues || []).forEach((c, idx) => {
        let x0 = timeToX(c.t, w);
        let x1 = timeToX(c.t + Math.max(0.04, c.hold_sec || 0.05), w);
        if (x1 < -4 || x0 > w + 4) return;
        if (x1 - x0 < 10) x1 = x0 + 10;
        const lane = idx % 3;
        const y0 = rh + wh + 6 + lane * ((ch - 8) / 3);
        const y1 = y0 + (ch - 8) / 3 - 4;
        const selected = selectedCueIds.has(c.cue_id);
        const warn = ovIds.has(c.cue_id);
        ctx.fillStyle = c.tile?.led_r != null
          ? `rgb(${c.tile.led_r},${c.tile.led_g},${c.tile.led_b})`
          : "#3d6ebd";
        ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
        if (warn) {
          ctx.fillStyle = "rgba(255, 180, 40, 0.35)";
          ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
        }
        ctx.strokeStyle = selected ? "#fff" : warn ? "#ffb400" : "#0d1117";
        ctx.lineWidth = selected || warn ? 2 : 1;
        ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
        if (x1 - x0 >= 28) {
          ctx.fillStyle = "#fff";
          ctx.font = "bold 11px Segoe UI";
          const label = `${warn ? "⚠ " : ""}${String(c.title || "").slice(0, 14)}`;
          ctx.fillText(label, x0 + 6, (y0 + y1) / 2 + 4);
        }
      });
      // playhead
      const pos = state?.timeline?.position || 0;
      const px = timeToX(pos, w);
      ctx.strokeStyle = "#ff5c5c";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(px, 0);
      ctx.lineTo(px, h);
      ctx.stroke();
      ctx.fillStyle = "#ff5c5c";
      ctx.beginPath();
      ctx.moveTo(px - 6, 0);
      ctx.lineTo(px + 6, 0);
      ctx.lineTo(px, 10);
      ctx.fill();
    }
    $("zoomLabel").textContent = `${Math.round(zoom * 100)}%`;
  }

  function hitCue(x, y) {
    const cv = $("tlCanvas");
    const w = cv.width;
    const h = cv.height;
    const rh = 22;
    const wh = 72;
    const ch = h - rh - wh;
    const cues = state?.cues || [];
    for (let i = cues.length - 1; i >= 0; i--) {
      const c = cues[i];
      let x0 = timeToX(c.t, w);
      let x1 = timeToX(c.t + Math.max(0.04, c.hold_sec || 0.05), w);
      if (x1 - x0 < 10) x1 = x0 + 10;
      const lane = i % 3;
      const y0 = rh + wh + 6 + lane * ((ch - 8) / 3);
      const y1 = y0 + (ch - 8) / 3 - 4;
      if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return c;
    }
    return null;
  }

  // ---- tabs ----
  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      const panel = $(`tab-${btn.dataset.tab}`);
      if (panel) panel.classList.add("active");
      if (btn.dataset.tab === "timeline") drawTimeline();
      if (btn.dataset.tab === "esp32") {
        // display:none 中に描いたパレットが黒いままになる対策
        if (esp32Palette) {
          esp32Palette.setRgb(
            Number($("esp32R")?.value || 255),
            Number($("esp32G")?.value || 80),
            Number($("esp32B")?.value || 40)
          );
        }
        fillEsp32PatternPresets(
          state?.esp32?.patterns?.length ? state.esp32.patterns : ESP32_PATTERN_FALLBACK,
          state?.esp32?.pattern_running ? state.esp32.pattern_id : ""
        );
        fillEsp32ColorPresets(state?.ble?.color_presets || ESP32_COLOR_FALLBACK);
        renderEsp32();
      }
    });
  });

  // ---- events ----
  $("audioFile").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    $("connStatus").textContent = "読込中…";
    try {
      const res = await fetch("/api/timeline/upload", { method: "POST", body: fd });
      const j = await res.json();
      if (!res.ok) throw new Error(j.detail || "upload failed");
      if (j.state) state = j.state;
      else await refreshState();
      renderAll();
      $("connStatus").textContent = "WebSocket 接続済";
    } catch (err) {
      alert(err.message || err);
    }
  });

  $("btnPlay").addEventListener("click", () =>
    api("/api/timeline/toggle", { method: "POST" })
      .then((j) => {
        if (state?.timeline) state.timeline.playing = j.playing;
        updateTransport();
      })
      .catch(alert)
  );
  $("btnStop").addEventListener("click", () =>
    api("/api/timeline/stop", { method: "POST" })
      .then(() => api("/api/timeline/seek", { method: "POST", body: JSON.stringify({ t: 0 }) }))
      .then(refreshState)
      .catch(alert)
  );

  $("volume").addEventListener("input", (e) => {
    api("/api/timeline/volume", {
      method: "POST",
      body: JSON.stringify({ volume: Number(e.target.value) / 100 }),
    }).catch(() => {});
  });

  function applyZoom(factor, anchorT) {
    const dur = state?.timeline?.duration || 0;
    if (dur <= 0) return;
    const oldVis = visibleDur();
    const anchor = anchorT ?? (state?.timeline?.position || viewStart + oldVis * 0.5);
    const frac = oldVis > 1e-9 ? Math.min(1, Math.max(0, (anchor - viewStart) / oldVis)) : 0.5;
    zoom = Math.max(1, Math.min(64, zoom * factor));
    const newVis = Math.max(0.05, dur / zoom);
    viewStart = anchor - frac * newVis;
    viewStart = Math.max(0, Math.min(Math.max(0, dur - newVis), viewStart));
    api("/api/timeline/zoom", {
      method: "POST",
      body: JSON.stringify({ zoom, view_start: viewStart }),
    }).catch(() => {});
    drawTimeline();
  }
  $("zoomIn").addEventListener("click", () => applyZoom(1.4));
  $("zoomOut").addEventListener("click", () => applyZoom(1 / 1.4));
  $("zoomFit").addEventListener("click", () => {
    zoom = 1;
    viewStart = 0;
    applyZoom(1, 0);
  });

  const cv = $("tlCanvas");
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const t = xToTime(x, cv.width);
    applyZoom(e.deltaY < 0 ? 1.25 : 1 / 1.25, t);
  }, { passive: false });

  cv.addEventListener("pointerdown", (e) => {
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    if (e.shiftKey) {
      panDrag = true;
      panAnchorX = x;
      panAnchorStart = viewStart;
      cv.setPointerCapture(e.pointerId);
      return;
    }
    const hit = hitCue(x, y);
    if (hit) {
      selectedCueIds = new Set([hit.cue_id]);
      renderCues();
      api("/api/timeline/seek", { method: "POST", body: JSON.stringify({ t: hit.t }) }).catch(() => {});
      drawTimeline();
      return;
    }
    seekDrag = true;
    cv.setPointerCapture(e.pointerId);
    const t = xToTime(x, cv.width);
    if (state?.timeline) state.timeline.position = t;
    updateTimeLabel();
    drawTimeline();
  });
  cv.addEventListener("pointermove", (e) => {
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (panDrag) {
      const vis = visibleDur();
      const dx = x - panAnchorX;
      const dur = state?.timeline?.duration || 0;
      viewStart = panAnchorStart - (dx / Math.max(1, cv.width)) * vis;
      viewStart = Math.max(0, Math.min(Math.max(0, dur - vis), viewStart));
      drawTimeline();
      return;
    }
    if (seekDrag) {
      const t = xToTime(x, cv.width);
      if (state?.timeline) state.timeline.position = t;
      updateTimeLabel();
      drawTimeline();
    }
  });
  cv.addEventListener("pointerup", (e) => {
    if (panDrag) {
      panDrag = false;
      api("/api/timeline/zoom", {
        method: "POST",
        body: JSON.stringify({ zoom, view_start: viewStart }),
      }).catch(() => {});
      return;
    }
    if (seekDrag) {
      seekDrag = false;
      const rect = cv.getBoundingClientRect();
      const t = xToTime(e.clientX - rect.left, cv.width);
      api("/api/timeline/seek", { method: "POST", body: JSON.stringify({ t }) }).catch(() => {});
    }
  });

  $("chkRecord").addEventListener("change", (e) =>
    api("/api/timeline/flags", {
      method: "POST",
      body: JSON.stringify({ record: e.target.checked }),
    }).catch(() => {})
  );
  $("chkAutoplay").addEventListener("change", (e) =>
    api("/api/timeline/flags", {
      method: "POST",
      body: JSON.stringify({ autoplay: e.target.checked }),
    }).catch(() => {})
  );

  $("btnDelCue").addEventListener("click", deleteSelectedCues);
  $("btnCueSelectAll").addEventListener("click", () => {
    const list = $("cueList");
    [...list.options].forEach((o) => {
      o.selected = true;
    });
    selectedCueIds = new Set([...list.options].map((o) => o.value));
  });
  $("btnCueUndo").addEventListener("click", undoCues);
  $("btnCueRedo").addEventListener("click", redoCues);
  $("cueList").addEventListener("change", () => {
    selectedCueIds = new Set([...$("cueList").selectedOptions].map((o) => o.value));
    drawTimeline();
  });
  $("btnClearCues").addEventListener("click", () => {
    if (!confirm("キューを全消去しますか？")) return;
    api("/api/cues/clear", { method: "POST" })
      .then((j) => {
        if (j.state) state = j.state;
        selectedCueIds = new Set();
        renderCues();
        drawTimeline();
      })
      .catch(alert);
  });

  $("btnSaveProj").addEventListener("click", () => {
    const name = prompt("プロジェクト名（空で自動）", "") || null;
    api("/api/project/save", { method: "POST", body: JSON.stringify({ name }) })
      .then((j) => alert(`保存しました\n${j.project}`))
      .catch(alert);
  });
  $("btnLoadProj").addEventListener("click", async () => {
    try {
      const j = await api("/api/project/list");
      if (!j.projects?.length) {
        alert("保存済みプロジェクトがありません");
        return;
      }
      const names = j.projects.map((p, i) => `${i + 1}. ${p.name}`).join("\n");
      const pick = prompt(`開く番号:\n${names}`, "1");
      const idx = Number(pick) - 1;
      if (!j.projects[idx]) return;
      await api("/api/project/load", {
        method: "POST",
        body: JSON.stringify({ path: j.projects[idx].path }),
      });
      await refreshState();
    } catch (err) {
      alert(err.message || err);
    }
  });

  // DMX
  $("btnDmxRefresh").addEventListener("click", () =>
    api("/api/dmx/ports").then((ports) => {
      if (state) state.dmx = { ...(state.dmx || {}), ports };
      renderDmx();
    })
  );
  $("btnDmxConnect").addEventListener("click", () =>
    api("/api/dmx/connect", {
      method: "POST",
      body: JSON.stringify({
        port: $("comPorts").value,
        base_addr: Number($("baseAddr").value) || 1,
      }),
    })
      .then((j) => {
        if (j.state) state = j.state;
        else return refreshState();
        renderAll();
      })
      .catch(alert)
  );
  $("btnDmxDisconnect").addEventListener("click", () =>
    api("/api/dmx/disconnect", { method: "POST" }).then(refreshState).catch(alert)
  );
  function setDmxAuto(enabled) {
    api("/api/dmx/auto-connect", {
      method: "POST",
      body: JSON.stringify({
        enabled: !!enabled,
        port: $("comPorts").value || undefined,
      }),
    })
      .then((j) => {
        if (j.state) mergeState(j.state);
        renderDmx();
        renderAi();
      })
      .catch(alert);
  }
  $("chkDmxAuto").addEventListener("change", (e) => setDmxAuto(e.target.checked));
  $("chkDmxAuto2").addEventListener("change", (e) => setDmxAuto(e.target.checked));

  $("btnMotionStart").addEventListener("click", () =>
    api("/api/dmx/motion/start", {
      method: "POST",
      body: JSON.stringify({
        index: Number($("motionSelect").value) || 0,
        speed: Number($("motionSpeed").value),
      }),
    }).catch(alert)
  );
  $("btnMotionStop").addEventListener("click", () =>
    api("/api/dmx/motion/stop", { method: "POST" }).catch(alert)
  );
  $("btnBlackout").addEventListener("click", () => {
    $("chSliders").querySelectorAll("input").forEach((el) => {
      el.value = "0";
      const sp = el.parentElement.querySelector(".ch-val");
      if (sp) sp.textContent = "0";
    });
    pushChannels();
    api("/api/dmx/motion/stop", { method: "POST" }).catch(() => {});
  });
  $("chkDotBase").addEventListener("change", (e) =>
    api("/api/dmx/dot-base", {
      method: "POST",
      body: JSON.stringify({ enabled: e.target.checked }),
    }).catch(alert)
  );
  ["syLo", "syHi"].forEach((id) =>
    $(id).addEventListener("input", () => {
      $("syLoVal").textContent = $("syLo").value;
      $("syHiVal").textContent = $("syHi").value;
    })
  );
  $("btnHeightApply").addEventListener("click", () =>
    api("/api/dmx/height", {
      method: "POST",
      body: JSON.stringify({
        lo_pct: Number($("syLo").value),
        hi_pct: Number($("syHi").value),
      }),
    })
      .then((j) => {
        if (j.state) mergeState(j.state);
        renderDmx();
      })
      .catch(alert)
  );

  // BLE
  $("btnBleScan").addEventListener("click", async () => {
    $("bleStatus").textContent = "スキャン中…";
    try {
      const j = await api("/api/ble/scan", { method: "POST" });
      if (state) state.ble = { ...(state.ble || {}), devices: j.devices };
      renderBle();
    } catch (err) {
      alert(err.message || err);
    }
  });
  function bleConnect(address) {
    const addr = (address || "").trim();
    if (!addr) {
      alert("BLE アドレスを入力するか、スキャン結果から選んでください");
      return;
    }
    $("bleStatus").textContent = `接続中… ${addr}`;
    api("/api/ble/connect", {
      method: "POST",
      body: JSON.stringify({ address: addr }),
    })
      .then((j) => {
        if (j.state) state = j.state;
        else return refreshState();
        renderAll();
      })
      .catch((err) => {
        alert(err.message || err);
        refreshState();
      });
  }
  $("btnBleConnect").addEventListener("click", () =>
    bleConnect($("bleDevices").value || $("bleAddress").value)
  );
  $("btnBleAddrConnect").addEventListener("click", () => bleConnect($("bleAddress").value));
  $("btnBleDisconnect").addEventListener("click", () =>
    api("/api/ble/disconnect", { method: "POST" }).then(refreshState).catch(alert)
  );
  function setBleAuto(enabled) {
    api("/api/ble/auto-connect", {
      method: "POST",
      body: JSON.stringify({
        enabled: !!enabled,
        address: $("bleAddress").value.trim() || undefined,
      }),
    })
      .then((j) => {
        if (j.state) mergeState(j.state);
        renderBle();
        renderAi();
      })
      .catch(alert);
  }
  $("chkBleAuto").addEventListener("change", (e) => setBleAuto(e.target.checked));
  $("chkBleAuto2").addEventListener("change", (e) => setBleAuto(e.target.checked));
  $("btnBleOn").addEventListener("click", () =>
    api("/api/ble/power", { method: "POST", body: JSON.stringify({ on: true }) }).catch(alert)
  );
  $("btnBleOff").addEventListener("click", () =>
    api("/api/ble/power", { method: "POST", body: JSON.stringify({ on: false }) }).catch(alert)
  );
  $("ledBright").addEventListener("input", updateSwatch);
  $("btnLedApply").addEventListener("click", () =>
    api("/api/ble/color", {
      method: "POST",
      body: JSON.stringify({
        r: Number($("ledR").value),
        g: Number($("ledG").value),
        b: Number($("ledB").value),
        brightness: Number($("ledBright").value) / 100,
      }),
    }).catch(alert)
  );
  $("btnBleMode").addEventListener("click", () =>
    api("/api/ble/mode", {
      method: "POST",
      body: JSON.stringify({
        mode_id: Number($("bleMode").value),
        speed: Number($("bleModeSpeed").value),
      }),
    }).catch(alert)
  );
  // ESP32 PixelCtl
  function applyEsp32State(j) {
    if (j?.state) mergeState(j.state);
    renderEsp32();
  }
  $("btnEsp32Save")?.addEventListener("click", () =>
    ensureEsp32Ready({ enable: false }).then(() => alert("設定を保存しました")).catch(alert)
  );
  $("btnEsp32Probe")?.addEventListener("click", () =>
    ensureEsp32Ready({ enable: false })
      .then(() => api("/api/esp32/probe", { method: "POST" }))
      .then((j) => {
        applyEsp32State(j);
        if (!j.ok) alert(j.error || "HTTP 未応答（Art-Net は使える場合あり）");
        else alert("疎通OK");
      })
      .catch(alert)
  );
  $("btnEsp32Enable")?.addEventListener("click", () =>
    ensureEsp32Ready({ enable: true }).then(() => applyEsp32State({ state })).catch(alert)
  );
  $("btnEsp32Disable")?.addEventListener("click", () =>
    api("/api/esp32/enable", {
      method: "POST",
      body: JSON.stringify({ enabled: false }),
    })
      .then(applyEsp32State)
      .catch(alert)
  );
  $("chkEsp32Auto")?.addEventListener("change", () =>
    ensureEsp32Ready({ enable: false }).catch(alert)
  );
  $("esp32PatSpeed")?.addEventListener("input", (e) => {
    const v = Number(e.target.value) / 100;
    if ($("esp32PatSpeedVal")) $("esp32PatSpeedVal").textContent = `${v.toFixed(2)}×`;
  });
  $("btnEsp32Apply")?.addEventListener("click", () => {
    if (esp32Selected.length) {
      livePaintEsp32Selection();
      return;
    }
    ensureEsp32Ready({ enable: true })
      .then(() =>
        api("/api/esp32/color", {
          method: "POST",
          body: JSON.stringify({
            r: Number($("esp32R").value),
            g: Number($("esp32G").value),
            b: Number($("esp32B").value),
            brightness: Number($("esp32Bright").value) / 100,
          }),
        })
      )
      .then(applyEsp32State)
      .catch(alert);
  });
  $("btnEsp32PatStop")?.addEventListener("click", () =>
    api("/api/esp32/pattern/stop", { method: "POST" }).then(applyEsp32State).catch(alert)
  );
  $("btnEsp32Blackout")?.addEventListener("click", () =>
    ensureEsp32Ready({ enable: true })
      .then(() => api("/api/esp32/blackout", { method: "POST" }))
      .then(applyEsp32State)
      .catch(alert)
  );
  $("btnEsp32SegApply")?.addEventListener("click", () => {
    const count = Math.max(1, Math.min(64, Number($("esp32SegCount")?.value) || 8));
    ensureEsp32Ready({ enable: true })
      .then(() =>
        api("/api/esp32/segments/count", {
          method: "POST",
          body: JSON.stringify({ count }),
        })
      )
      .then((j) => {
        esp32Selected = [];
        esp32SelAnchor = null;
        esp32ActiveGroupSpans = null;
        applyEsp32State(j);
      })
      .catch(alert);
  });
  $("btnEsp32SelAll")?.addEventListener("click", () => {
    const n = Number(state?.esp32?.segment_count || $("esp32SegCount")?.value || 8);
    esp32Selected = Array.from({ length: n }, (_, i) => i);
    esp32SelAnchor = 0;
    esp32ActiveGroupSpans = null;
    if (state?.esp32) state.esp32.selected = [...esp32Selected];
    renderEsp32Segments(state?.esp32 || {});
    pushEsp32Selection();
    livePaintEsp32Selection();
  });
  $("btnEsp32SelClear")?.addEventListener("click", () => {
    esp32Selected = [];
    esp32SelAnchor = null;
    esp32ActiveGroupSpans = null;
    if (state?.esp32) state.esp32.selected = [];
    renderEsp32Segments(state?.esp32 || {});
    pushEsp32Selection();
  });
  $("btnEsp32SegAllOff")?.addEventListener("click", () =>
    ensureEsp32Ready({ enable: true })
      .then(() =>
        api("/api/esp32/segments/fill", {
          method: "POST",
          body: JSON.stringify({ r: 0, g: 0, b: 0, on: false }),
        })
      )
      .then(applyEsp32State)
      .catch(alert)
  );
  $("btnEsp32GroupSave")?.addEventListener("click", () => {
    if (!esp32Selected.length) {
      alert("先に区間を選択してください");
      return;
    }
    const name = ($("esp32GroupName")?.value || "").trim() || `グループ${(state?.esp32?.groups?.length || 0) + 1}`;
    const gid = $("esp32GroupName")?.dataset?.gid || null;
    const segCount = Number(state?.esp32?.segment_count || $("esp32SegCount")?.value || 8);
    const pixelCount = Number(state?.esp32?.pixel_count || $("esp32Pixels")?.value || 720);
    api("/api/esp32/groups", {
      method: "POST",
      body: JSON.stringify({
        name,
        indices: esp32Selected,
        group_id: gid,
        segment_count: segCount,
        pixel_count: pixelCount,
      }),
    })
      .then((j) => {
        if (j.group && $("esp32GroupName")) {
          $("esp32GroupName").dataset.gid = j.group.id;
          $("esp32GroupName").value = j.group.name;
        }
        if (j.group?.spans) {
          esp32ActiveGroupSpans = j.group.spans.map((s) => [Number(s[0]), Number(s[1])]);
        }
        // グループ一覧を強制再描画
        const box = $("esp32GroupList");
        if (box) delete box.dataset.sig;
        applyEsp32State(j);
      })
      .catch(alert);
  });
  $("btnEsp32GroupDelete")?.addEventListener("click", () => {
    const gid = $("esp32GroupName")?.dataset?.gid;
    if (!gid) {
      alert("削除するグループを一覧から選んでください");
      return;
    }
    api("/api/esp32/groups/delete", {
      method: "POST",
      body: JSON.stringify({ group_id: gid }),
    })
      .then((j) => {
        if ($("esp32GroupName")) {
          $("esp32GroupName").value = "";
          delete $("esp32GroupName").dataset.gid;
        }
        const box = $("esp32GroupList");
        if (box) delete box.dataset.sig;
        applyEsp32State(j);
      })
      .catch(alert);
  });
  $("esp32Bright")?.addEventListener("input", () => {
    updateEsp32Swatch();
    livePaintEsp32Selection();
  });
  $("teEsp32Target")?.addEventListener("change", syncTeEsp32TargetUi);
  $("teEsp32Fx")?.addEventListener("change", syncTeEsp32TargetUi);
  $("teEsp32PatSpeed")?.addEventListener("input", (e) => {
    if ($("teEsp32PatSpeedVal")) {
      $("teEsp32PatSpeedVal").textContent = `${(Number(e.target.value) / 100).toFixed(2)}×`;
    }
  });
  $("btnTeEsp32UseSelection")?.addEventListener("click", () => {
    if (!esp32Selected.length) {
      alert("ESP32 タブで区間を選択してから取り込んでください");
      return;
    }
    const segCount = Number(state?.esp32?.segment_count || $("esp32SegCount")?.value || 8);
    const ranges = indicesToFracRanges(esp32Selected, segCount);
    if ($("teEsp32Segments")) $("teEsp32Segments").value = esp32Selected.join(",");
    if ($("teEsp32SegCount")) $("teEsp32SegCount").value = String(segCount);
    if ($("teEsp32Ranges")) $("teEsp32Ranges").value = JSON.stringify(ranges);
    if ($("teEsp32Target")) $("teEsp32Target").value = "segments";
    syncTeEsp32TargetUi();
  });

  $("bleMode").addEventListener("change", () => {
    const mid = Number($("bleMode").value);
    if (mid < 0) {
      // soft fx → color flash
      api("/api/ble/color", {
        method: "POST",
        body: JSON.stringify({
          r: Number($("ledR").value),
          g: Number($("ledG").value),
          b: Number($("ledB").value),
          brightness: Number($("ledBright").value) / 100,
        }),
      }).catch(alert);
      return;
    }
    api("/api/ble/mode", {
      method: "POST",
      body: JSON.stringify({
        mode_id: mid,
        speed: Number($("bleModeSpeed").value),
      }),
    }).catch(alert);
  });

  // Tile editor / sidebar
  $("tileEditList").addEventListener("change", () => {
    editTileIndex = Number($("tileEditList").value) || 0;
    const t = (state?.tiles || [])[editTileIndex];
    renderTileSummary(t || null);
    $("tileEditStatus").textContent = t
      ? `#${editTileIndex + 1} / ${state.tiles.length}`
      : "—";
  });
  $("tileEditList").addEventListener("dblclick", () => {
    if ((state?.tiles || []).length) openTileSidebar("edit", editTileIndex);
  });
  $("teLaser").addEventListener("change", syncDevicePanels);
  $("teLed").addEventListener("change", syncDevicePanels);
  if ($("teEsp32")) $("teEsp32").addEventListener("change", syncDevicePanels);
  $("teSpeed").addEventListener("input", (e) => {
    $("teSpeedVal").textContent = Number(e.target.value).toFixed(2);
  });
  $("teLedSpeed").addEventListener("input", (e) => {
    $("teLedSpeedVal").textContent = e.target.value;
  });
  $("teLedBright").addEventListener("input", (e) => {
    $("teLedBrightVal").textContent = e.target.value;
  });

  $("btnTileNew").addEventListener("click", () => openTileSidebar("new"));
  $("btnTileEdit").addEventListener("click", () => {
    if (!(state?.tiles || []).length) {
      alert("編集するタイルがありません");
      return;
    }
    openTileSidebar("edit", Number($("tileEditList").value) || 0);
  });
  $("btnTileSidebarClose").addEventListener("click", closeTileSidebar);
  $("btnTileSidebarCancel").addEventListener("click", closeTileSidebar);
  $("tileSidebarBackdrop").addEventListener("click", closeTileSidebar);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("tileSidebar").hidden) {
      e.preventDefault();
      closeTileSidebar();
    }
  });

  $("btnTileSidebarSave").addEventListener("click", () => {
    if (!$("teLaser").checked && !$("teLed").checked && !$("teEsp32")?.checked) {
      alert("DMX / BLE / ESP32 のいずれかを選んでください");
      return;
    }
    const tile = readTileFromForm();
    const index = sidebarMode === "new" ? null : editTileIndex;
    api("/api/tiles/save", {
      method: "POST",
      body: JSON.stringify({ index, tile }),
    })
      .then((j) => {
        if (j.state) state = j.state;
        else if (j.tiles) state.tiles = j.tiles;
        editTileIndex = j.index ?? editTileIndex;
        closeTileSidebar();
        renderTileEditor();
        renderTiles();
        $("tileEditStatus").textContent =
          sidebarMode === "new" ? "作成しました" : "保存しました";
      })
      .catch(alert);
  });

  function bindPreview(btn) {
    if (!btn) return;
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const tile =
        btn.id === "btnTileSidebarPreview"
          ? readTileFromForm()
          : (state?.tiles || [])[editTileIndex];
      if (!tile) return;
      api("/api/tiles/hold", {
        method: "POST",
        body: JSON.stringify({ source: "preview", tile }),
      }).catch(alert);
    });
    const release = () => {
      api("/api/tiles/release", {
        method: "POST",
        body: JSON.stringify({ source: "preview" }),
      }).catch(() => {});
    };
    btn.addEventListener("pointerup", release);
    btn.addEventListener("pointerleave", release);
    btn.addEventListener("pointercancel", release);
  }
  bindPreview($("btnTileSidebarPreview"));
  bindPreview($("btnTilePreview"));

  $("btnTileDelete").addEventListener("click", () => {
    if (!(state?.tiles || []).length) return;
    editTileIndex = Number($("tileEditList").value) || 0;
    if (!confirm("このタイルを削除しますか？")) return;
    api("/api/tiles/delete", {
      method: "POST",
      body: JSON.stringify({ index: editTileIndex }),
    })
      .then((j) => {
        if (j.state) state = j.state;
        editTileIndex = Math.max(0, editTileIndex - 1);
        renderTileEditor();
        renderTiles();
      })
      .catch(alert);
  });
  $("btnTileCapture").addEventListener("click", () => {
    openTileSidebar("new");
    // キャプチャ相当: 現在の DMX をフォームに載せたうえで機器はユーザーが選ぶ
    defaultNewTileForm();
    syncDevicePanels();
    $("tileSidebarTitle").textContent = "現在をキャプチャ";
    $("btnTileSidebarSave").textContent = "キャプチャして作成";
  });
  $("btnTileLedPack").addEventListener("click", () =>
    api("/api/tiles/led-pack", { method: "POST" })
      .then((j) => {
        if (j.state) state = j.state;
        alert(`${j.added || 0} 件追加しました`);
        renderTileEditor();
        renderTiles();
      })
      .catch(alert)
  );
  $("btnTileReload").addEventListener("click", () =>
    api("/api/tiles")
      .then((j) => {
        if (state) state.tiles = j.tiles;
        renderTileEditor();
        renderTiles();
      })
      .catch(alert)
  );

  // AI
  $("aiSens").addEventListener("input", (e) => {
    $("aiSensVal").textContent = Number(e.target.value).toFixed(1);
  });
  $("btnAiDevRefresh").addEventListener("click", () =>
    api("/api/ai/devices")
      .then((j) => {
        if (j.state) state = j.state;
        renderAi();
      })
      .catch(alert)
  );
  $("aiDevice").addEventListener("change", () => {
    const opt = $("aiDevice").selectedOptions[0];
    if (!opt) return;
    const id = opt.value === "default" ? null : Number(opt.value);
    api("/api/ai/device", {
      method: "POST",
      body: JSON.stringify({ device_id: id, label: opt.dataset.label || opt.textContent }),
    })
      .then((j) => {
        if (j.state) mergeState(j.state);
        renderAi();
      })
      .catch(alert);
  });
  $("aiMode").addEventListener("change", () =>
    api("/api/ai/mode", {
      method: "POST",
      body: JSON.stringify({ mode: $("aiMode").value }),
    }).catch(alert)
  );
  $("btnAiStart").addEventListener("click", () =>
    api("/api/ai/start", {
      method: "POST",
      body: JSON.stringify({
        sensitivity: Number($("aiSens").value),
        mode: $("aiMode").value,
      }),
    })
      .then((j) => {
        if (j.state) state = j.state;
        else return refreshState();
        renderAll();
      })
      .catch(alert)
  );
  $("btnAiStop").addEventListener("click", () =>
    api("/api/ai/stop", { method: "POST" }).then(refreshState).catch(alert)
  );

  // Space + hotkeys + cue history
  window.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    const inText = tag === "INPUT" || tag === "TEXTAREA";
    const inCueList = e.target === $("cueList");

    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) {
        if (!inText) {
          e.preventDefault();
          undoCues();
        }
        return;
      }
      if (k === "y" || (k === "z" && e.shiftKey)) {
        if (!inText) {
          e.preventDefault();
          redoCues();
        }
        return;
      }
    }

    if (inText) return;

    if (inCueList) {
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        deleteSelectedCues();
      }
      return;
    }
    if (tag === "SELECT") return;

    if (e.code === "Space") {
      e.preventDefault();
      if (e.repeat) return;
      api("/api/timeline/toggle", { method: "POST" })
        .then((j) => {
          if (state?.timeline) state.timeline.playing = j.playing;
          updateTransport();
        })
        .catch(() => {});
      return;
    }
    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      deleteSelectedCues();
      return;
    }
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const tiles = state?.tiles || [];
    const idx = tiles.findIndex((t) => t.hotkey && String(t.hotkey).toLowerCase() === key);
    if (idx >= 0 && !heldKeys.has(key)) {
      heldKeys.add(key);
      api("/api/tiles/hold", {
        method: "POST",
        body: JSON.stringify({ source: key, tile_index: idx }),
      }).catch(() => {});
    }
  });
  window.addEventListener("keyup", (e) => {
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (heldKeys.has(key)) {
      heldKeys.delete(key);
      api("/api/tiles/release", {
        method: "POST",
        body: JSON.stringify({ source: key }),
      }).catch(() => {});
    }
  });

  window.addEventListener("resize", () => drawTimeline());

  try {
    tePalette = createLedPalette(
      "teLedSv", "teLedHue", "teLedR", "teLedG", "teLedB", "teLedSwatch", "teLedRgbLabel"
    );
  } catch (_) {}
  try {
    blePalette = createLedPalette(
      "bleLedSv", "bleLedHue", "ledR", "ledG", "ledB", "ledSwatch", "bleLedRgbLabel"
    );
  } catch (_) {}
  try {
    esp32Palette = createLedPalette(
      "esp32Sv",
      "esp32Hue",
      "esp32R",
      "esp32G",
      "esp32B",
      "esp32Swatch",
      "esp32RgbLabel",
      () => livePaintEsp32Selection()
    );
  } catch (_) {}

  // 状態取得前でもプリセットを必ず表示
  fillEsp32PatternPresets(ESP32_PATTERN_FALLBACK, "");
  fillEsp32ColorPresets(ESP32_COLOR_FALLBACK);

  connectWs();
  refreshState().catch((err) => {
    $("connStatus").textContent = `エラー: ${err.message || err}`;
  });
})();
