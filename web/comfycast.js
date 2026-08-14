// ComfyCast — "Run on Remote" button, connection light, settings panel,
// live progress, preview + full-res fetch.
// Deliberately framework-light: floating elements only, so it works across
// ComfyUI frontend versions.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const css = `
#comfycast-wrap { position: fixed; top: 8px; right: 130px; z-index: 9999;
  display: flex; gap: 6px; align-items: center; font: 600 13px sans-serif; }
#comfycast-btn {
  padding: 6px 14px; border-radius: 6px; border: 1px solid #4a8;
  background: #1d2b25; color: #9fe8c5; cursor: pointer; font: inherit;
  display: flex; align-items: center; gap: 8px;
}
#comfycast-btn:hover { background: #24382f; }
#comfycast-btn.busy { border-color: #ca5; color: #f0d9a0; background: #2b271d; cursor: default; }
#comfycast-dot { width: 9px; height: 9px; border-radius: 50%; background: #666;
  box-shadow: 0 0 4px rgba(0,0,0,.4); flex: none; }
#comfycast-dot.ok { background: #3fbf6f; box-shadow: 0 0 6px #3fbf6f88; }
#comfycast-dot.bad { background: #d05252; box-shadow: 0 0 6px #d0525288; }
#comfycast-gear {
  padding: 6px 9px; border-radius: 6px; border: 1px solid #3a424b;
  background: #21262c; color: #cfd4da; cursor: pointer; font: inherit;
}
#comfycast-gear:hover { background: #2a3138; }
#comfycast-mode {
  padding: 6px 10px; border-radius: 6px; border: 1px solid #3a424b;
  background: #21262c; color: #9aa2ab; cursor: pointer; font: inherit;
}
#comfycast-mode.box { border-color: #4a8; background: #1d2b25; color: #9fe8c5; }
#comfycast-mode:hover { filter: brightness(1.15); }
#comfycast-panel {
  position: fixed; top: 46px; right: 130px; z-index: 9999; width: 340px;
  background: #16191d; color: #cfd4da; border: 1px solid #333a42;
  border-radius: 8px; padding: 10px 12px; font: 12px/1.5 sans-serif;
  box-shadow: 0 6px 24px rgba(0,0,0,.5);
}
#comfycast-panel h4 { margin: 0 0 6px; font-size: 12px; color: #9fe8c5; }
#comfycast-panel .ck-err { color: #f08d8d; white-space: pre-wrap; }
#comfycast-panel .ck-ok { color: #8fd8ab; }
#comfycast-panel .ck-bar { height: 6px; background: #262c33; border-radius: 3px; margin: 6px 0; }
#comfycast-panel .ck-bar > div { height: 6px; border-radius: 3px; background: #4a8; width: 0%; transition: width .2s; }
#comfycast-panel img { max-width: 100%; border-radius: 6px; margin-top: 6px; }
#comfycast-panel .ck-row { display: flex; gap: 8px; margin-top: 8px; }
#comfycast-panel button {
  flex: 1; padding: 5px 8px; border-radius: 5px; border: 1px solid #3a424b;
  background: #21262c; color: #cfd4da; cursor: pointer; font: 600 12px sans-serif;
}
#comfycast-panel button:hover { background: #2a3138; }
#comfycast-panel .ck-dim { color: #7c848d; }
#comfycast-panel label { display: block; margin-top: 8px; color: #9aa2ab; }
#comfycast-panel input {
  width: 100%; box-sizing: border-box; margin-top: 3px; padding: 5px 7px;
  border-radius: 5px; border: 1px solid #3a424b; background: #0f1215;
  color: #dfe4e9; font: 12px monospace;
}
`;

let pollTimer = null;
let pingTimer = null;
let remoteAddr = null;  // shown on the button so you know WHICH machine you're on

function btnLabel(busy) {
  const who = remoteAddr || "Remote";
  return busy ? ` Running on ${who}…` : ` Run on ${who} ▶`;
}

// Re-dispatch remote execution events into the local frontend, so the canvas
// shows the same green running-node outline / red error node as a local run.
function mirror(type, detail) {
  try {
    window.__comfycast_last_mirror = { type, detail };
    if (api.dispatchCustomEvent) api.dispatchCustomEvent(type, detail);
    else api.dispatchEvent(new CustomEvent(type, { detail }));
  } catch (e) { /* frontend variant without this event — cosmetic only */ }
}

function el(tag, attrs = {}, html = "") {
  const e = document.createElement(tag);
  Object.assign(e, attrs);
  if (html) e.innerHTML = html;
  return e;
}

function panel() {
  let p = document.getElementById("comfycast-panel");
  if (!p) {
    p = el("div", { id: "comfycast-panel" });
    document.body.appendChild(p);
  }
  p.style.display = "block";
  return p;
}

function hidePanel() {
  const p = document.getElementById("comfycast-panel");
  if (p) p.style.display = "none";
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function setBusy(b) {
  const btn = document.getElementById("comfycast-btn");
  if (btn) {
    btn.classList.toggle("busy", b);
    btn.childNodes[1].textContent = btnLabel(b);
  }
}

function setDot(state, title) {
  const dot = document.getElementById("comfycast-dot");
  if (!dot) return;
  dot.className = state ? (state === "ok" ? "ok" : "bad") : "";
  dot.id = "comfycast-dot";
  if (title) document.getElementById("comfycast-btn").title = title;
}

// Escape a LEAF DATA VALUE for interpolation into innerHTML. Applied to values
// only — never to the HTML fragments this file builds deliberately (frac,
// queue, extra, tb, html), and never stacked on something already escaped.
// Three untrusted sources reach the panel: the config (settable over an
// unauthenticated route), workflow node titles (hostile workflow JSON is a
// normal thing to be sent), and error text from the remote or ssh stderr.
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(n) {
  if (n == null) return "?";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}

async function ping(showResultIn) {
  try {
    const r = await api.fetchApi("/comfycast/ping");
    const d = await r.json();
    if (d.ok) {
      setDot("ok", `remote: ComfyUI ${d.version} — ${d.gpu} (${fmtBytes(d.vram_free)} VRAM free)`);
      if (showResultIn) showResultIn.innerHTML =
        `<span class="ck-ok">✓ connected — ComfyUI ${esc(d.version)}, ${esc(d.gpu)}, ${fmtBytes(d.vram_free)} VRAM free</span>`;
      return true;
    }
    setDot("bad", "remote unreachable: " + d.error);
    if (showResultIn) showResultIn.innerHTML = `<span class="ck-err">✗ ${esc(d.error)}</span>`;
  } catch (e) {
    setDot("bad", "remote unreachable");
    if (showResultIn) showResultIn.innerHTML = `<span class="ck-err">✗ ${esc(e.message || e)}</span>`;
  }
  return false;
}

async function openSettings() {
  const p = panel();
  let cfg = { address: "", port: 8188, ssh_host: "", preview: "webp;80" };
  try {
    const r = await api.fetchApi("/comfycast/config");
    cfg = await r.json();
    remoteAddr = cfg.address || (cfg.ssh_host ? cfg.ssh_host.split("@").pop() : null);
  } catch (e) { /* defaults */ }
  p.innerHTML = `<h4>ComfyCast — remote connection</h4>
    <label>Remote address — Tailscale IP or hostname of the machine with the GPU</label>
    <input id="ck-addr" placeholder="e.g. 100.x.y.z or my-linux-box" value="${esc(cfg.address)}"/>
    <label>ComfyUI port on the remote</label>
    <input id="ck-port" value="${esc(cfg.port) || 8188}"/>
    <label>SSH fallback (user@host) — optional, used if the address doesn't answer</label>
    <input id="ck-ssh" placeholder="e.g. me@my-linux-box" value="${esc(cfg.ssh_host)}"/>
    <label>Preview quality (format;quality — smaller = less bandwidth)</label>
    <input id="ck-prev" value="${esc(cfg.preview) || "webp;80"}"/>
    <div class="ck-row">
      <button id="ck-save">Save &amp; test</button>
      <button id="ck-close">close</button>
    </div>
    <div id="ck-save-out" class="ck-dim" style="margin-top:6px"></div>`;
  document.getElementById("ck-close").onclick = hidePanel;
  document.getElementById("ck-save").onclick = async () => {
    const out = document.getElementById("ck-save-out");
    out.textContent = "saving…";
    try {
      const r = await api.fetchApi("/comfycast/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          address: document.getElementById("ck-addr").value,
          port: parseInt(document.getElementById("ck-port").value) || 8188,
          ssh_host: document.getElementById("ck-ssh").value,
          preview: document.getElementById("ck-prev").value,
        }),
      });
      if (!r.ok) throw new Error((await r.json()).error || "save failed");
      const a = document.getElementById("ck-addr").value.trim();
      const sh = document.getElementById("ck-ssh").value.trim();
      remoteAddr = a || (sh ? sh.split("@").pop() : null);
      setBusy(false);  // refresh the button label with the new address
      out.textContent = "saved — testing connection…";
      const ok = await ping(out);
      if (ok) {
        // connecting a machine catalogues its models automatically (per-machine cache)
        const prog = document.createElement("div");
        out.parentElement.appendChild(prog);
        pollSync(prog, () => {});
      }
    } catch (e) {
      out.innerHTML = `<span class="ck-err">✗ ${esc(e.message || e)}</span>`;
    }
  };
}

function setModeButton(active, extra) {
  const m = document.getElementById("comfycast-mode");
  if (!m) return;
  m.classList.toggle("box", !!active);
  m.textContent = active ? "models: REMOTE" : "models: LOCAL";
  m.title = (active
    ? "Dropdowns show every model on the remote (zero-byte stubs). Click for LOCAL: only models really on this machine."
    : "Dropdowns show only models really on this machine. Click for REMOTE: author with the remote's full model list (works offline via cache).")
    + (extra ? "\n" + extra : "");
}

async function refreshCombos() {
  try {
    if (app.refreshComboInNodes) await app.refreshComboInNodes();
  } catch (e) { /* older frontend — user presses R */ }
}

async function pollSync(renderInto, onDone) {
  // poll /comfycast/stubs/state while a sync runs; draw a real progress bar
  const timer = setInterval(async () => {
    let st;
    try {
      const r = await api.fetchApi("/comfycast/stubs/state");
      st = await r.json();
    } catch (e) { return; }
    const sy = st.syncing || {};
    if (sy.running) {
      const pct = sy.total ? Math.round((100 * sy.done) / sy.total) : 5;
      renderInto.innerHTML = `<div>${esc(sy.phase) || "working"}${sy.total ? ` — ${sy.done}/${sy.total}` : ""}</div>
        <div class="ck-bar"><div style="width:${pct}%"></div></div>`;
    } else {
      clearInterval(timer);
      if (sy.error) renderInto.innerHTML = `<span class="ck-err">✗ ${esc(sy.error)}</span>`;
      else renderInto.innerHTML = `<span class="ck-ok">✓ ${esc(sy.phase) || "done"}</span>`;
      onDone && onDone(st, sy);
    }
  }, 300);
}

async function toggleMode() {
  const m = document.getElementById("comfycast-mode");
  const goingActive = !m.classList.contains("box");
  const p = panel();
  p.innerHTML = `<h4>ComfyCast — models</h4><div id="ck-sync"></div>`;
  try {
    const r = await api.fetchApi("/comfycast/stubs/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: goingActive }),
    });
    if (!r.ok) throw new Error((await r.json()).error || "toggle failed");
  } catch (e) {
    p.innerHTML = `<h4>ComfyCast — models</h4><div class="ck-err">${esc(e.message || e)}</div>
      <div class="ck-row"><button onclick="this.closest('#comfycast-panel').style.display='none'">close</button></div>`;
    return;
  }
  pollSync(document.getElementById("ck-sync"), async (st, sy) => {
    if (!sy.error) {
      setModeButton(st.active, st.active_machine ? `connected catalogue: ${st.active_machine}` : null);
      await refreshCombos();
      setTimeout(hidePanel, 1800);
    }
  });
}

async function runOnBox() {
  if (document.getElementById("comfycast-btn")?.classList.contains("busy")) return;
  const p = panel();
  p.innerHTML = "<h4>ComfyCast</h4><div class='ck-dim'>exporting graph…</div>";
  let prompt;
  try {
    const g = await app.graphToPrompt();
    prompt = g.output;
  } catch (e) {
    p.innerHTML = `<h4>ComfyCast</h4><div class="ck-err">couldn't export graph: ${esc(e)}</div>`;
    return;
  }
  setBusy(true);
  p.innerHTML = "<h4>ComfyCast</h4><div class='ck-dim'>sending to the remote…</div>";
  const token = Math.random().toString(36).slice(2);
  // while the submit request runs, poll its progress (input-file uploads)
  const subTimer = setInterval(async () => {
    try {
      const r = await api.fetchApi(`/comfycast/submit_progress?token=${token}`);
      const d = await r.json();
      if (!d.phase) return;
      if (d.phase === "uploading inputs" && d.total) {
        const pct = d.bytes_total ? Math.round((100 * d.bytes_done) / d.bytes_total) : 0;
        p.innerHTML = `<h4>ComfyCast</h4>
          <div>uploading input files the remote doesn't have — ${d.done}/${d.total}</div>
          <div class="ck-dim">${esc(d.file)} (${fmtBytes(d.bytes_done)} of ${fmtBytes(d.bytes_total)})</div>
          <div class="ck-bar"><div style="width:${pct}%"></div></div>`;
      } else {
        p.innerHTML = `<h4>ComfyCast</h4><div class='ck-dim'>${esc(d.phase)}…</div>`;
      }
    } catch (e) {}
  }, 400);
  let res;
  try {
    const r = await api.fetchApi("/comfycast/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, token }),
    });
    res = await r.json();
    if (!r.ok) {
      let msg = res.error || "submit failed";
      if (res.detail?.node_errors) {
        for (const [nid, ne] of Object.entries(res.detail.node_errors)) {
          msg += `\n${ne.class_type || "node " + nid}: ` +
            (ne.errors || []).map((e) => e.message + (e.details ? ` [${e.details}]` : "")).join("; ");
        }
      } else if (res.detail?.error?.message) {
        msg += `\n${res.detail.error.message}`;
      }
      throw new Error(msg);
    }
  } catch (e) {
    clearInterval(subTimer);
    setBusy(false);
    p.innerHTML = `<h4>ComfyCast</h4><div class="ck-err">${esc(e.message || e)}</div>
      <div class="ck-row"><button onclick="this.closest('#comfycast-panel').style.display='none'">close</button></div>`;
    return;
  }
  clearInterval(subTimer);
  watch(res.prompt_id);
}

function watch(pid) {
  const p = panel();
  if (pollTimer) clearInterval(pollTimer);
  let lastMirrored;
  mirror("execution_start", { prompt_id: pid });
  pollTimer = setInterval(async () => {
    let s;
    try {
      const r = await api.fetchApi(`/comfycast/status?pid=${pid}`);
      s = await r.json();
    } catch (e) { return; }
    if (s.status === "running") {
      if (s.current != null && String(s.current) !== lastMirrored) {
        lastMirrored = String(s.current);
        mirror("executing", lastMirrored);
      }
      if (s.max) mirror("progress", { value: s.value, max: s.max,
                                      prompt_id: pid, node: lastMirrored });
      const frac = s.max ? ` — ${s.value}/${s.max}` : "";
      const queue = s.queue_ahead ? `<div class="ck-dim">${s.queue_ahead} job(s) ahead in queue</div>` : "";
      const pct = s.max ? Math.round((100 * s.value) / s.max)
        : Math.round((100 * s.done) / Math.max(s.total, 1));
      const mm = (n) => `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
      let extra = `<div class="ck-dim">elapsed ${mm(s.elapsed || 0)}</div>`;
      if (s.age > 10) extra += `<div class="ck-dim">quiet for ${s.age}s — big model loads and VAE
        decodes report no progress; the run is still going on the remote.</div>`;
      if (s.ws === false) extra += `<div class="ck-dim">live feed unavailable on this connection —
        checking the remote every couple of seconds instead. Even if this device drops offline,
        the run finishes on the remote (reopen later and Fetch).</div>`;
      p.innerHTML = `<h4>ComfyCast — running</h4>${queue}
        <div>[${Math.min(s.done + 1, s.total)}/${s.total}] ${esc(s.current_label) || "…"}${frac}</div>
        <div class="ck-bar"><div style="width:${pct}%"></div></div>${extra}
        <div class="ck-row"><button id="ck-stop" style="border-color:#a55;color:#f0a0a0">■ Stop</button></div>`;
      const stopBtn = document.getElementById("ck-stop");
      if (stopBtn) stopBtn.onclick = async () => {
        stopBtn.textContent = "stopping…"; stopBtn.disabled = true;
        try { await api.fetchApi(`/comfycast/stop?pid=${pid}`, { method: "POST" }); } catch (e) {}
      };
    } else if (s.status === "error") {
      clearInterval(pollTimer); pollTimer = null; setBusy(false);
      mirror("execution_error", {
        prompt_id: pid, node_id: s.error_node, node_type: s.error_node_type,
        exception_type: s.error_type || "Error",
        exception_message: s.error_msg || s.error,
        traceback: (s.error_tb || "").split("\n"),
      });
      const tb = s.error_tb
        ? `<pre class="ck-dim" style="max-height:140px;overflow:auto;font-size:10px;margin:6px 0 0;white-space:pre-wrap">${esc(s.error_tb)}</pre>`
        : "";
      p.innerHTML = `<h4>ComfyCast — error on the remote</h4><div class="ck-err">${esc(s.error) || "unknown error"}</div>${tb}
        <div class="ck-row"><button onclick="this.closest('#comfycast-panel').style.display='none'">close</button></div>`;
    } else if (s.status === "done") {
      clearInterval(pollTimer); pollTimer = null; setBusy(false);
      mirror("executing", null);
      mirror("execution_success", { prompt_id: pid });
      const outs = s.outputs || [];
      const imgs = outs.filter((o) => o.kind === "images");
      const others = outs.length - imgs.length;
      let html = `<h4>ComfyCast — done ✓</h4>`;
      if (imgs.length) {
        html += `<img src="/comfycast/preview?pid=${encodeURIComponent(pid)}&idx=0&t=${Date.now()}" alt="preview"/>`;
        if (imgs.length > 1) html += `<div class="ck-dim">${imgs.length} images — showing first (small preview)</div>`;
        else html += `<div class="ck-dim">small preview — full resolution is still on the remote</div>`;
      }
      if (others > 0) html += `<div class="ck-dim">${others} non-image output(s) (video/audio) — Fetch downloads them full-size</div>`;
      if (!outs.length) {
        html += `<div class="ck-dim">run finished — no downloadable outputs in history.
          Nodes that write files directly (EXR savers etc.) put them in the remote's
          output folder; grab them with <b>comfycast fetch</b> or your file sync.</div>
          <div class="ck-row"><button onclick="this.closest('#comfycast-panel').style.display='none'">close</button></div>`;
        p.innerHTML = html;
        return;
      }
      html += `<div class="ck-row">
        <button id="ck-fetch">Fetch full-res</button>
        <button onclick="this.closest('#comfycast-panel').style.display='none'">close</button></div>
        <div id="ck-fetch-out" class="ck-dim"></div>`;
      p.innerHTML = html;
      document.getElementById("ck-fetch").onclick = async () => {
        const out = document.getElementById("ck-fetch-out");
        out.textContent = "downloading full resolution…";
        try {
          const r = await api.fetchApi(`/comfycast/fetch?pid=${pid}`);
          const d = await r.json();
          if (!r.ok) throw new Error(d.error || "fetch failed");
          out.textContent = `saved ${d.files.length} file(s), ${fmtBytes(d.bytes)} → ${d.dir}`;
        } catch (e) { out.textContent = "fetch failed: " + (e.message || e); }
      };
    }
  }, 700);
}

app.registerExtension({
  name: "comfycast.runonbox",
  async setup() {
    document.head.appendChild(el("style", {}, css));
    const wrap = el("div", { id: "comfycast-wrap" });
    const btn = el("button", { id: "comfycast-btn", title: "Send the current workflow to your remote machine (comfycast)" });
    btn.appendChild(el("span", { id: "comfycast-dot" }));
    btn.appendChild(document.createTextNode(" Run on Remote ▶"));
    btn.onclick = runOnBox;
    const mode = el("button", { id: "comfycast-mode" });
    mode.textContent = "models: …";
    mode.onclick = toggleMode;
    const gear = el("button", { id: "comfycast-gear", title: "ComfyCast remote connection settings" });
    gear.textContent = "⚙";
    gear.onclick = openSettings;
    wrap.appendChild(btn);
    wrap.appendChild(mode);
    wrap.appendChild(gear);
    document.body.appendChild(wrap);

    // learn the configured address for the button label
    try {
      const rc = await api.fetchApi("/comfycast/config");
      const c = await rc.json();
      remoteAddr = c.address || (c.ssh_host ? c.ssh_host.split("@").pop() : null);
      setBusy(false);
    } catch (e) { /* keep generic label */ }

    // models-toggle initial state
    try {
      const r = await api.fetchApi("/comfycast/stubs/state");
      const d = await r.json();
      const m = d.machines && d.machine && d.machines[d.machine];
      setModeButton(d.active, m ? `${m.count} models catalogued for ${d.machine} (${m.updated})` : null);
    } catch (e) { setModeButton(false); }

    // connection light: check now, then every 30s
    const configured = await (async () => {
      try {
        const r = await api.fetchApi("/comfycast/config");
        return (await r.json()).configured;
      } catch (e) { return false; }
    })();
    if (!configured) {
      setDot("bad", "not configured — click ⚙ to set your remote address");
      openSettings();
    } else {
      ping(null);
    }
    pingTimer = setInterval(() => ping(null), 30000);
  },
});
