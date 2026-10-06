/* Andonstar local control UI */

const $ = (sel) => document.querySelector(sel);

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.hidden = false;
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3200);
}

function fillSelect(sel, options, current) {
  sel.innerHTML = "";
  for (const [value, label] of options) {
    const opt = document.createElement("option");
    opt.value = String(value);
    opt.textContent = label;
    if (String(current) === String(value)) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.error || res.statusText || "request failed");
  }
  if (data.ok === false && path !== "/api/state") {
    throw new Error(data.error || "request failed");
  }
  return data;
}

function fillIfaceSelect(ifaces, preferred) {
  const sel = $("#wifi-iface");
  const cur = sel.value;
  sel.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "(auto)";
  sel.appendChild(empty);
  for (const iface of ifaces || []) {
    const opt = document.createElement("option");
    opt.value = iface.name;
    const ip = (iface.ipv4 || []).join(",") || "no-ip";
    opt.textContent = `${iface.name} — ${iface.state || "?"} — ${iface.ssid || "(none)"} — ${ip}`;
    sel.appendChild(opt);
  }
  // Prefer: saved selection, else Wi-Fi 2 / USB-looking disconnected, else Andonstar link
  let pick = preferred || cur || "";
  if (!pick) {
    const usb = (ifaces || []).find((i) => /wi-?fi\s*2|usb|tp-link/i.test(`${i.name} ${i.description || ""}`));
    const andon = (ifaces || []).find((i) => (i.ssid || "").toLowerCase().startsWith("andonstar"));
    pick = (andon || usb || {}).name || "";
  }
  if (pick) sel.value = pick;
}

function fillSsidSelect(networks, keep) {
  const sel = $("#wifi-ssid");
  const previous = keep || sel.value;
  sel.innerHTML = "";
  const list = networks || [];
  if (!list.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no Andonstar SSIDs — scan again)";
    sel.appendChild(opt);
    return;
  }
  for (const n of list) {
    const opt = document.createElement("option");
    opt.value = n.ssid;
    opt.textContent = `${n.ssid}  ${n.signal || ""}`.trim();
    sel.appendChild(opt);
  }
  if (previous && [...sel.options].some((o) => o.value === previous)) {
    sel.value = previous;
  }
}

function renderLink(link) {
  $("#wifi-box").textContent = JSON.stringify(link || {}, null, 2);
  if (link && link.interfaces) {
    fillIfaceSelect(link.interfaces, link.preferred);
  }
  const summary = $("#wifi-summary-status");
  if (!summary) return;
  if (link && link.scope_reachable) {
    const active = link.active;
    const ssid = (active && active.ssid) || (link.andonstar_links && link.andonstar_links[0] && link.andonstar_links[0].ssid) || "scope";
    summary.textContent = `online · ${ssid}`;
  } else if (link && link.andonstar_links && link.andonstar_links.length) {
    summary.textContent = `linked · unreachable`;
  } else {
    const disconnected = (link && link.interfaces || []).find((i) => /wi-?fi\s*2|usb|tp-link/i.test(`${i.name} ${i.description || ""}`));
    summary.textContent = disconnected ? `${disconnected.name} idle` : "offline";
  }
}

async function wifiScan() {
  const iface = $("#wifi-iface").value || undefined;
  const q = iface ? `?interface=${encodeURIComponent(iface)}` : "";
  const data = await api(`/api/wifi/scan${q}`);
  const andon = data.andonstar && data.andonstar.length ? data.andonstar : (data.networks || []).filter((n) => (n.ssid || "").toLowerCase().startsWith("andonstar"));
  fillSsidSelect(andon.length ? andon : data.networks || []);
  toast(andon.length ? `found ${andon.length} Andonstar SSID(s)` : "scan done — no Andonstar SSID yet");
  return data;
}

function bindSet(sel, cmd) {
  sel.addEventListener("change", async () => {
    try {
      await api("/api/set", {
        method: "POST",
        body: JSON.stringify({ cmd, par: Number(sel.value) }),
      });
      toast(`cmd ${cmd} → ${sel.value}`);
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });
}

function setStreamImg(on) {
  const img = $("#preview");
  const empty = $("#preview-empty");
  if (on) {
    img.src = `/stream?ts=${Date.now()}`;
    empty.classList.add("hidden");
  } else {
    img.removeAttribute("src");
    empty.classList.remove("hidden");
    empty.textContent = "Stream off";
  }
}

function renderModes(options, currentMode) {
  const wrap = $("#mode-buttons");
  wrap.innerHTML = "";
  const cur = currentMode == null || currentMode === "" ? null : String(currentMode);
  for (const [value, label] of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    if (cur !== null && cur === String(value)) btn.classList.add("active");
    btn.addEventListener("click", async () => {
      try {
        const r = await api("/api/mode", {
          method: "POST",
          body: JSON.stringify({ mode: Number(value) }),
        });
        setStreamImg(!!r.stream_enabled);
        renderModes(options, r.current_mode ?? value);
        toast(`mode ${label}`);
        await refresh();
      } catch (e) {
        toast(String(e.message || e), true);
      }
    });
    wrap.appendChild(btn);
  }
}

async function refresh() {
  const state = await api("/api/state");
  const conn = $("#conn");
  if (state.ok) {
    conn.textContent = "scope online";
    conn.className = "pill ok";
  } else {
    conn.textContent = state.error || "offline";
    conn.className = "pill bad";
  }
  $("#scope-ip").textContent = state.ip || "";
  const direct = state.stream_url || "#";
  $("#obs-link").href = direct;
  $("#obs-link").textContent = direct.replace(/^https?:\/\//, "");

  renderLink(state.link);

  const cfg = state.config || {};
  const opts = state.options || {};
  fillSelect($("#photo-res"), opts.photo_res || [], cfg["1002"]);
  fillSelect($("#video-res"), opts.video_res || [], cfg["2002"]);
  fillSelect($("#video-res-alt"), opts.video_res_alt || [], cfg["2024"]);
  fillSelect($("#exposure"), opts.exposure || [], cfg["2005"]);
  fillSelect($("#brightness"), opts.brightness || [], cfg["2020"]);
  fillSelect($("#flip"), opts.flip || [], cfg["2023"]);
  fillSelect($("#loop"), opts.loop_record || [], cfg["2003"]);
  if (cfg["2004"] != null) $("#hdr").value = String(cfg["2004"]);
  if (cfg["2008"] != null) $("#watermark").value = String(cfg["2008"]);

  const modeStatus = state.current_mode != null ? state.current_mode : null;
  renderModes(opts.modes || [], modeStatus);

  $("#status-box").textContent = JSON.stringify(
    {
      stream_enabled: state.stream_enabled,
      proxy_clients: state.proxy_clients,
      mode: state.mode,
      wifi: state.wifi,
      disk: state.disk,
      device: state.device,
      config: state.config,
    },
    null,
    2,
  );

  if (state.stream_enabled) {
    const img = $("#preview");
    if (!img.src || !img.src.includes("/stream")) setStreamImg(true);
    $("#preview-empty").classList.add("hidden");
  }
  return state;
}

function wire() {
  bindSet($("#photo-res"), 1002);
  bindSet($("#video-res"), 2002);
  bindSet($("#video-res-alt"), 2024);
  bindSet($("#exposure"), 2005);
  bindSet($("#brightness"), 2020);
  bindSet($("#flip"), 2023);
  bindSet($("#hdr"), 2004);
  bindSet($("#watermark"), 2008);
  bindSet($("#loop"), 2003);

  $("#btn-stream-on").addEventListener("click", async () => {
    try {
      await api("/api/preview", { method: "POST", body: JSON.stringify({ on: true }) });
      setStreamImg(true);
      toast("stream enabled");
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-stream-off").addEventListener("click", async () => {
    try {
      setStreamImg(false);
      await api("/api/preview", { method: "POST", body: JSON.stringify({ on: false }) });
      toast("stream disabled");
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-refresh").addEventListener("click", () => refresh().catch((e) => toast(String(e.message || e), true)));

  $("#btn-wifi-scan").addEventListener("click", () => {
    wifiScan().catch((e) => toast(String(e.message || e), true));
  });

  $("#wifi-iface").addEventListener("change", async () => {
    try {
      await api("/api/wifi/prefer", {
        method: "POST",
        body: JSON.stringify({ interface: $("#wifi-iface").value || null }),
      });
    } catch (_) { /* ignore */ }
  });

  $("#btn-wifi-connect").addEventListener("click", async () => {
    try {
      const ssid = $("#wifi-ssid").value;
      if (!ssid) {
        await wifiScan();
      }
      const ssid2 = $("#wifi-ssid").value;
      if (!ssid2) throw new Error("No SSID selected — turn on microscope Wi‑Fi and Scan");
      toast("connecting…");
      const r = await api("/api/wifi/connect", {
        method: "POST",
        body: JSON.stringify({
          ssid: ssid2,
          password: $("#wifi-pass").value || "12345678",
          interface: $("#wifi-iface").value || null,
        }),
      });
      renderLink(r.link);
      toast(r.ok ? `connected to ${ssid2}` : (r.connect || r.error || "connect failed"), !r.ok);
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-wifi-disconnect").addEventListener("click", async () => {
    try {
      const r = await api("/api/wifi/disconnect", {
        method: "POST",
        body: JSON.stringify({ interface: $("#wifi-iface").value || null }),
      });
      renderLink(r.link);
      toast(r.ok ? "disconnected" : (r.output || r.error || "disconnect failed"), !r.ok);
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-wifi-route").addEventListener("click", async () => {
    try {
      const r = await api("/api/wifi/route", {
        method: "POST",
        body: JSON.stringify({ interface: $("#wifi-iface").value || null }),
      });
      renderLink(r.link);
      toast(r.ok ? "host route set" : (r.error || r.output || "route failed"), !r.ok);
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-snap").addEventListener("click", async () => {
    try {
      const r = await api("/api/snap", { method: "POST", body: "{}" });
      toast(`snap ${JSON.stringify(r.response)}`);
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-rec-on").addEventListener("click", async () => {
    try {
      await api("/api/record", { method: "POST", body: JSON.stringify({ on: true }) });
      toast("recording on");
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-rec-off").addEventListener("click", async () => {
    try {
      await api("/api/record", { method: "POST", body: JSON.stringify({ on: false }) });
      toast("recording off");
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });

  $("#btn-raw").addEventListener("click", async () => {
    const cmd = Number($("#raw-cmd").value);
    const parRaw = $("#raw-par").value.trim();
    const body = { cmd };
    if (parRaw !== "") body.par = /^\d+$/.test(parRaw) ? Number(parRaw) : parRaw;
    try {
      const r = await api("/api/set", { method: "POST", body: JSON.stringify(body) });
      toast(JSON.stringify(r.response));
      await refresh();
    } catch (e) {
      toast(String(e.message || e), true);
    }
  });
}

wire();
refresh()
  .then(() => wifiScan().catch(() => {}))
  .catch((e) => toast(String(e.message || e), true));
setInterval(() => {
  refresh().catch(() => {});
}, 8000);
