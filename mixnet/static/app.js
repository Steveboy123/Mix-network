(() => {
  'use strict';

  const POLL_MS = 1000;
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;

  let state = null;
  let failures = 0;
  let hoverIndex = null;
  let chartGeom = null;
  let lastBannerKey = '';
  let lastSpeedKey = '';
  const pendingWeight = new Map(); // link name -> weight being dragged
  const cards = new Map(); // link name -> card refs
  const otherRows = new Map();

  const MODE_DESC = {
    speed: 'Spreads connections across every online link for the most total speed. A higher weight gets a bigger share.',
    backup: 'Sends everything over your highest-weight link. The others stand by and take over within seconds if it drops.',
    latency: 'Sends each new connection over the link with the lowest ping. Good for games and video calls.',
  };

  // ------------------------------------------------------------------ formatting

  function rate(bps) {
    if (!bps || bps < 1) return { v: '0', u: 'Mbps' };
    if (bps >= 1e9) return { v: (bps / 1e9).toFixed(2), u: 'Gbps' };
    if (bps >= 1e6) {
      const m = bps / 1e6;
      return { v: m >= 100 ? m.toFixed(0) : m.toFixed(1), u: 'Mbps' };
    }
    if (bps >= 1e3) return { v: (bps / 1e3).toFixed(0), u: 'Kbps' };
    return { v: bps.toFixed(0), u: 'bps' };
  }
  const rateText = (bps) => { const r = rate(bps); return `${r.v} ${r.u}`; };
  const mbpsText = (m) => (m >= 100 ? m.toFixed(0) : m.toFixed(1));

  function bytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return `${n} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
  }

  // ------------------------------------------------------------------ icons (static markup only)

  const ICONS = {
    wifi: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 8.8a14 14 0 0 1 19 0"/><path d="M5.5 12.3a9.5 9.5 0 0 1 13 0"/><path d="M8.7 15.7a5 5 0 0 1 6.6 0"/><circle cx="12" cy="19" r="0.9"/></svg>',
    ethernet: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M8.5 19v-3.5h7V19"/><path d="M7.5 9.5v2M10.5 9.5v2M13.5 9.5v2M16.5 9.5v2"/></svg>',
    usb: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M10.5 18.5h3"/></svg>',
    cellular: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 19v-3M10 19v-7M15 19V8M20 19V4"/></svg>',
    bluetooth: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7.5l10 9-5 4.5V3l5 4.5-10 9"/></svg>',
    vpn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></svg>',
    other: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/></svg>',
  };
  const iconFor = (kind) => ICONS[kind] || ICONS.other;

  const STATUS = {
    online: { label: 'Online', color: 'var(--good)', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M4.8 8.2l2.1 2.1 4.3-4.4" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>' },
    offline: { label: 'No internet', color: 'var(--critical)', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>' },
    warning: { label: '', color: 'var(--warning)', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5l7 12.5H1z" fill="currentColor"/><path d="M8 6v3.6" stroke="#0b0b0b" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.8" r="0.9" fill="#0b0b0b"/></svg>' },
    checking: { label: 'Checking…', color: 'var(--muted)', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="3 3"/></svg>' },
    paused: { label: 'Off', color: 'var(--muted)', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M6.3 5.5v5M9.7 5.5v5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>' },
    disconnected: { label: 'Disconnected', color: 'var(--muted)', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M4.5 8h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>' },
  };

  function setStatusIcon(el, key) {
    const s = STATUS[key];
    el.innerHTML = s.svg;
    el.style.color = s.color;
  }

  // ------------------------------------------------------------------ colors

  const cssVar = (name) => getComputedStyle(root).getPropertyValue(name).trim();
  const seriesColor = (l) => cssVar(`--series-${l.color % 8}`);
  function withAlpha(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  }

  // ------------------------------------------------------------------ api

  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-MixNet': '1' },
      body: JSON.stringify(body),
    };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  async function act(path, body) {
    try {
      render(await api(path, body));
    } catch (err) {
      toast(err.message);
    }
  }

  let toastTimer = null;
  function toast(message) {
    let el = document.querySelector('.toast');
    if (!el) {
      el = document.createElement('div');
      el.className = 'toast';
      el.setAttribute('role', 'status');
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 4000);
  }

  async function poll() {
    try {
      const s = await api('/api/state');
      failures = 0;
      $('offline').hidden = true;
      render(s);
    } catch (err) {
      failures += 1;
      if (failures >= 3) $('offline').hidden = false;
    } finally {
      setTimeout(poll, POLL_MS);
    }
  }

  // ------------------------------------------------------------------ helpers

  const inMix = (l) => !l.virtual || l.enabled;
  const linkState = (l) => (!l.present ? 'disconnected' : !l.enabled ? 'paused' : l.status);

  function subline(l) {
    const parts = [];
    if (!l.label.toLowerCase().includes(l.kind_label.toLowerCase())) parts.push(l.kind_label);
    if (l.ipv4 || l.ipv6) parts.push(l.ipv4 || l.ipv6);
    if (l.speed_mbps) parts.push(l.speed_mbps >= 1000 ? `${l.speed_mbps / 1000} Gbps link` : `${l.speed_mbps} Mbps link`);
    return parts.join(' · ');
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function swatch(color) {
    const s = el('span', 'swatch');
    s.style.background = color;
    return s;
  }

  function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, rect.width);
    const h = Math.max(1, rect.height);
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function niceScale(max) {
    const raw = max / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
    const top = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = 0; v <= top + step / 2; v += step) ticks.push(v);
    return { max: top, ticks };
  }

  // ------------------------------------------------------------------ render

  function render(s) {
    state = s;
    renderStatus(s);
    renderBanners(s);
    renderHero(s);
    renderLegend(s);
    drawMain();
    renderLinks(s);
    renderOthers(s);
    renderSpeedtest(s);
    renderUse(s);
    renderFlows(s);
  }

  function renderStatus(s) {
    const pill = $('statusPill');
    const used = s.links.filter((l) => l.present && l.enabled);
    const online = used.filter((l) => l.status === 'online').length;
    const checking = used.some((l) => l.status === 'checking');
    let key, text;
    if (online >= 2) { key = 'online'; text = `Mixing ${online} connections`; }
    else if (online === 1) { key = 'warning'; text = '1 connection online'; }
    else if (checking) { key = 'checking'; text = 'Checking connections…'; }
    else { key = 'offline'; text = 'No connection online'; }
    setStatusIcon(pill.querySelector('.status-icon'), key);
    pill.querySelector('.status-text').textContent = text;
  }

  function renderBanners(s) {
    const items = [];
    if (s.app.demo) items.push(['info', 'Demo mode: these connections and their traffic are simulated. Start MixNet without --demo to use your real network.']);
    for (const w of s.app.warnings) items.push(['warn', w]);
    if (s.proxy.error) items.push(['warn', s.proxy.error]);
    const real = s.links.filter((l) => !l.virtual && l.present);
    if (!s.app.demo && real.length < 2) {
      items.push(['info', real.length === 0
        ? 'No network connections found yet. Connect to Wi-Fi or plug in a cable.'
        : 'Only one connection found. To mix, add another: connect Wi-Fi and plug in Ethernet, or tether your phone over USB.']);
    }
    const key = JSON.stringify(items);
    if (key === lastBannerKey) return;
    lastBannerKey = key;
    const box = $('banners');
    box.replaceChildren();
    for (const [kind, text] of items) {
      const b = el('div', `banner ${kind === 'info' ? 'info' : ''}`);
      const icon = el('span');
      setStatusIcon(icon, kind === 'info' ? 'checking' : 'warning');
      if (kind === 'info') icon.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 7.2v4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="4.9" r="0.9" fill="currentColor"/></svg>';
      b.append(icon, el('div', '', text));
      box.append(b);
    }
  }

  function renderHero(s) {
    const d = rate(s.totals.rate_down);
    $('heroDown').textContent = d.v;
    $('heroDownUnit').textContent = d.u;
    $('heroUp').textContent = rateText(s.totals.rate_up);
    $('heroConns').textContent = String(s.totals.active);
    $('heroBytes').textContent = bytes(s.totals.bytes_down + s.totals.bytes_up);
    for (const b of $('modeGroup').querySelectorAll('button')) {
      const on = b.dataset.mode === s.mode;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    $('modeDesc').textContent = MODE_DESC[s.mode] || '';
  }

  function chartLinks() {
    if (!state) return [];
    return state.links.filter((l) => inMix(l) && (l.present || l.hist_down.some((v) => v > 0)));
  }

  function renderLegend(s) {
    const box = $('legend');
    box.replaceChildren();
    const total = s.totals.rate_down + s.totals.rate_up;
    for (const l of chartLinks()) {
      const item = el('span', `legend-item${l.enabled ? '' : ' off'}`);
      item.append(swatch(seriesColor(l)), el('span', '', l.label));
      if (total > 0) item.append(el('strong', '', `${Math.round(l.share * 100)}%`));
      box.append(item);
    }
  }

  function drawMain() {
    const canvas = $('mainChart');
    const { ctx, w, h } = setupCanvas(canvas);
    const links = chartLinks();
    const n = links.length ? links[0].hist_down.length : 90;
    const padL = 44, padR = 10, padT = 22, padB = 2;
    const pw = w - padL - padR;
    const ph = h - padT - padB;
    let acc = new Array(n).fill(0);
    const stacks = links.map((l) => {
      const top = acc.map((v, i) => v + (l.hist_down[i] || 0));
      const layer = { link: l, color: seriesColor(l), bottom: acc, top };
      acc = top;
      return layer;
    });
    const peak = Math.max(1e6, ...acc);
    const { max, ticks } = niceScale(peak * 1.08);
    const giga = max >= 2e9;
    const x = (i) => padL + (n <= 1 ? 0 : (i * pw) / (n - 1));
    const y = (v) => padT + ph - (v / max) * ph;
    chartGeom = { padL, pw, n, x, y, stacks, w };

    const grid = cssVar('--grid');
    const muted = cssVar('--muted');
    const surface = cssVar('--surface');
    ctx.font = '12px system-ui, -apple-system, "Segoe UI", sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = muted;
    ctx.lineWidth = 1;
    for (const t of ticks) {
      const yy = Math.round(y(t)) + 0.5;
      ctx.strokeStyle = t === 0 ? cssVar('--baseline') : grid;
      ctx.beginPath();
      ctx.moveTo(padL, yy);
      ctx.lineTo(w - padR, yy);
      ctx.stroke();
      const label = giga ? (t / 1e9).toLocaleString(undefined, { maximumFractionDigits: 1 })
        : (t / 1e6).toLocaleString(undefined, { maximumFractionDigits: 1 });
      ctx.fillText(label, padL - 8, yy);
    }
    ctx.textAlign = 'left';
    ctx.fillText(giga ? 'Gbps' : 'Mbps', 0, 8);

    // Area washes, bottom to top.
    for (const layer of stacks) {
      ctx.beginPath();
      ctx.moveTo(x(0), y(layer.top[0]));
      for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(layer.top[i]));
      for (let i = n - 1; i >= 0; i--) ctx.lineTo(x(i), y(layer.bottom[i]));
      ctx.closePath();
      ctx.fillStyle = withAlpha(layer.color, 0.18);
      ctx.fill();
    }
    // Edge lines, top layer first so a zero-height layer never hides the one beneath.
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    for (const layer of [...stacks].reverse()) {
      ctx.beginPath();
      ctx.moveTo(x(0), y(layer.top[0]));
      for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(layer.top[i]));
      ctx.strokeStyle = surface;
      ctx.lineWidth = 4;
      ctx.stroke();
      ctx.strokeStyle = layer.color;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    if (hoverIndex !== null && hoverIndex < n && stacks.length) {
      const xx = Math.round(x(hoverIndex)) + 0.5;
      ctx.strokeStyle = cssVar('--ink-2');
      ctx.globalAlpha = 0.5;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xx, padT);
      ctx.lineTo(xx, padT + ph);
      ctx.stroke();
      ctx.globalAlpha = 1;
      for (const layer of stacks) {
        ctx.beginPath();
        ctx.arc(x(hoverIndex), y(layer.top[hoverIndex]), 4, 0, Math.PI * 2);
        ctx.fillStyle = layer.color;
        ctx.strokeStyle = surface;
        ctx.lineWidth = 2;
        ctx.fill();
        ctx.stroke();
      }
    }
    if (hoverIndex !== null) updateTooltip();
  }

  function updateTooltip() {
    const tip = $('tooltip');
    if (hoverIndex === null || !chartGeom || !chartGeom.stacks.length) {
      tip.hidden = true;
      return;
    }
    const { stacks, n, x, w } = chartGeom;
    const i = Math.min(hoverIndex, n - 1);
    const ago = n - 1 - i;
    tip.replaceChildren(el('div', 'tt-title', ago === 0 ? 'Now' : `${ago} s ago`));
    let total = 0;
    for (const layer of [...stacks].reverse()) {
      const v = layer.link.hist_down[i] || 0;
      total += v;
      const row = el('div', 'tt-row');
      const key = el('span', 'tt-key');
      key.style.background = layer.color;
      row.append(key, el('span', '', layer.link.label), el('strong', '', rateText(v)));
      tip.append(row);
    }
    const tot = el('div', 'tt-row tt-total');
    tot.append(el('span'), el('span', '', 'Combined'), el('strong', '', rateText(total)));
    tip.append(tot);
    tip.hidden = false;
    const px = x(i);
    const tw = tip.offsetWidth;
    tip.style.left = `${px + 14 + tw > w ? px - 14 - tw : px + 14}px`;
  }

  function onChartPointer(ev) {
    if (!chartGeom) return;
    const rect = $('chartWrap').getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const { padL, pw, n } = chartGeom;
    if (px < padL - 6 || px > padL + pw + 6) { clearHover(); return; }
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - padL) / pw) * (n - 1))));
    if (i !== hoverIndex) {
      hoverIndex = i;
      drawMain();
    }
  }
  function clearHover() {
    if (hoverIndex === null) return;
    hoverIndex = null;
    $('tooltip').hidden = true;
    drawMain();
  }

  function drawSpark(canvas, values, color) {
    const { ctx, w, h } = setupCanvas(canvas);
    const pts = values.slice(-60);
    const n = pts.length;
    const max = Math.max(2e5, ...pts) * 1.1;
    const x = (i) => 4 + (i * (w - 10)) / Math.max(1, n - 1);
    const y = (v) => h - 2 - (v / max) * (h - 8);
    ctx.beginPath();
    ctx.moveTo(x(0), y(pts[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(pts[i]));
    ctx.lineTo(x(n - 1), h - 2);
    ctx.lineTo(x(0), h - 2);
    ctx.closePath();
    ctx.fillStyle = withAlpha(color, 0.14);
    ctx.fill();
    ctx.beginPath();
    ctx.moveTo(x(0), y(pts[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(pts[i]));
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x(n - 1), y(pts[n - 1]), 4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.strokeStyle = cssVar('--surface');
    ctx.lineWidth = 2;
    ctx.fill();
    ctx.stroke();
  }

  // ------------------------------------------------------------------ link cards

  function createCard(l) {
    const root = el('article', 'card link-card');
    const head = el('div', 'link-head');
    const icon = el('div', 'link-icon');
    icon.innerHTML = iconFor(l.kind);
    const sw = swatch('');
    icon.append(sw);
    const title = el('div', 'link-title');
    const name = el('div', 'link-name');
    const sub = el('div', 'link-sub');
    title.append(name, sub);
    const toggle = el('button', 'switch');
    toggle.type = 'button';
    toggle.setAttribute('role', 'switch');
    toggle.append(el('span'));
    toggle.addEventListener('click', () => {
      const cur = state.links.find((x) => x.name === l.name);
      act('/api/link', { name: l.name, enabled: !(cur && cur.enabled) });
    });
    head.append(icon, title, toggle);

    const status = el('div', 'link-status');
    const sIcon = el('span', 'status-icon');
    const sText = el('span');
    const sLat = el('span', 'lat');
    status.append(sIcon, sText, sLat);

    const stats = el('div', 'link-stats');
    const rateBox = el('div', 'link-rate');
    const rateV = el('span');
    const rateU = el('small');
    rateBox.append(rateV, rateU);
    const upBox = el('div', 'link-minor', 'Up ');
    const up = el('strong');
    upBox.append(up);
    const connBox = el('div', 'link-minor');
    const conns = el('strong');
    connBox.append(conns, document.createTextNode(' open'));
    stats.append(rateBox, upBox, connBox);

    const spark = el('canvas', 'spark');
    spark.setAttribute('aria-hidden', 'true');

    const foot = el('div', 'link-foot');
    const id = `w-${Math.random().toString(36).slice(2, 8)}`;
    const lab = el('label', '', 'Weight');
    lab.htmlFor = id;
    const slider = el('input');
    slider.type = 'range';
    slider.min = '1';
    slider.max = '10';
    slider.step = '1';
    slider.id = id;
    const out = el('output');
    out.htmlFor = id;
    slider.addEventListener('input', () => {
      pendingWeight.set(l.name, Number(slider.value));
      out.textContent = slider.value;
    });
    slider.addEventListener('change', async () => {
      await act('/api/link', { name: l.name, weight: Number(slider.value) });
      pendingWeight.delete(l.name);
    });
    const share = el('div', 'link-share', 'Share ');
    const shareV = el('strong');
    share.append(shareV);
    foot.append(lab, slider, out, share);

    root.append(head, status, stats, spark, foot);
    return { root, sw, name, sub, toggle, sIcon, sText, sLat, rateV, rateU, up, conns, spark, slider, out, shareV, icon, kind: l.kind };
  }

  function updateCard(c, l) {
    const color = seriesColor(l);
    const st = linkState(l);
    c.root.classList.toggle('paused', st === 'paused');
    c.root.classList.toggle('gone', st === 'disconnected');
    if (c.kind !== l.kind) {
      c.icon.innerHTML = iconFor(l.kind);
      c.icon.append(c.sw);
      c.kind = l.kind;
    }
    c.sw.style.background = color;
    c.name.textContent = l.label;
    c.name.title = l.label;
    c.sub.textContent = subline(l);
    c.toggle.setAttribute('aria-checked', String(l.enabled));
    c.toggle.setAttribute('aria-label', `Use ${l.label} in the mix`);
    c.toggle.disabled = !l.present && !l.enabled;
    setStatusIcon(c.sIcon, st);
    c.sText.textContent = STATUS[st].label;
    let lat = '';
    if (l.latency_ms !== null && l.present) lat = `${Math.round(l.latency_ms)} ms ping`;
    if (st === 'paused' && l.status === 'online') lat = `internet OK${lat ? ', ' + lat : ''}`;
    c.sLat.textContent = lat ? `· ${lat}` : '';
    const r = rate(l.rate_down);
    c.rateV.textContent = r.v;
    c.rateU.textContent = `${r.u} down`;
    c.up.textContent = rateText(l.rate_up);
    c.conns.textContent = String(l.active);
    if (!pendingWeight.has(l.name)) {
      c.slider.value = String(l.weight);
      c.out.textContent = String(l.weight);
    }
    c.slider.setAttribute('aria-label', `Weight for ${l.label}`);
    c.shareV.textContent = `${Math.round(l.share * 100)}%`;
    drawSpark(c.spark, l.hist_down, color);
  }

  function renderLinks(s) {
    const main = s.links.filter((l) => !l.virtual);
    const box = $('links');
    const names = new Set(main.map((l) => l.name));
    for (const [name, c] of cards) {
      if (!names.has(name)) {
        c.root.remove();
        cards.delete(name);
      }
    }
    let empty = box.querySelector('.links-empty');
    if (!main.length) {
      if (!empty) {
        empty = el('div', 'card links-empty', 'No network connections found. Connect to Wi-Fi or plug in a cable.');
        box.append(empty);
      }
      return;
    }
    if (empty) empty.remove();
    main.forEach((l, i) => {
      let c = cards.get(l.name);
      if (!c) {
        c = createCard(l);
        cards.set(l.name, c);
      }
      if (box.children[i] !== c.root) box.insertBefore(c.root, box.children[i] || null);
      updateCard(c, l);
    });
  }

  function renderOthers(s) {
    const others = s.links.filter((l) => l.virtual);
    $('others').hidden = others.length === 0;
    $('othersCount').textContent = others.length ? `(${others.length})` : '';
    const list = $('othersList');
    const names = new Set(others.map((l) => l.name));
    for (const [name, row] of otherRows) {
      if (!names.has(name)) { row.root.remove(); otherRows.delete(name); }
    }
    for (const l of others) {
      let r = otherRows.get(l.name);
      if (!r) {
        const root = el('div', 'other-row');
        const sw = swatch('');
        const grow = el('div', 'grow');
        const name = el('div', 'link-name');
        const sub = el('div', 'link-sub');
        grow.append(name, sub);
        const sIcon = el('span', 'status-icon');
        const toggle = el('button', 'switch');
        toggle.type = 'button';
        toggle.setAttribute('role', 'switch');
        toggle.append(el('span'));
        toggle.addEventListener('click', () => {
          const cur = state.links.find((x) => x.name === l.name);
          act('/api/link', { name: l.name, enabled: !(cur && cur.enabled) });
        });
        root.append(sw, grow, sIcon, toggle);
        list.append(root);
        r = { root, sw, name, sub, sIcon, toggle };
        otherRows.set(l.name, r);
      }
      const st = linkState(l);
      r.sw.style.background = l.enabled ? seriesColor(l) : 'var(--track)';
      r.name.textContent = l.label;
      r.sub.textContent = `${subline(l)} · ${STATUS[st].label}`;
      setStatusIcon(r.sIcon, st);
      r.toggle.setAttribute('aria-checked', String(l.enabled));
      r.toggle.setAttribute('aria-label', `Use ${l.label} in the mix`);
    }
  }

  // ------------------------------------------------------------------ speed test

  function renderSpeedtest(s) {
    const st = s.speedtest;
    const run = $('stRun');
    run.disabled = st.running;
    run.textContent = st.running ? 'Testing…' : st.results && st.results.finished ? 'Run again' : 'Run test';
    $('stProgress').hidden = !st.running;
    $('stPhase').textContent = st.phase || '';
    $('stFill').style.width = `${Math.round((st.progress || 0) * 100)}%`;
    $('stError').hidden = !st.error;
    $('stError').textContent = st.error || '';

    const res = st.results;
    const key = JSON.stringify([res, st.running, s.links.map((l) => [l.name, l.color, l.label])]);
    if (key === lastSpeedKey) return;
    lastSpeedKey = key;

    const bars = $('stBars');
    bars.replaceChildren();
    const foot = $('stFoot');
    if (!res) { foot.hidden = true; return; }
    const byName = new Map(s.links.map((l) => [l.name, l]));
    const entries = Object.entries(res.links || {});
    const values = entries.map(([, v]) => v);
    if (res.mixed) values.push(res.mixed);
    const max = Math.max(1, ...values);

    const row = (label, color, parts, total, isTotal) => {
      const r = el('div', 'bar-row');
      const lab = el('div', `bar-label${isTotal ? ' total' : ''}`);
      if (color) lab.append(swatch(color));
      lab.append(el('span', '', label));
      const track = el('div', 'bar-track');
      const fill = el('div', 'bar-fill');
      fill.style.width = `${Math.max(0.5, (total / max) * 78)}%`;
      for (const [c, v] of parts) {
        if (v <= 0) continue;
        const seg = el('i');
        seg.style.background = c;
        seg.style.flex = String(v);
        fill.append(seg);
      }
      const val = el('span', 'bar-value', mbpsText(total));
      val.append(el('small', '', ' Mbps'));
      track.append(fill, val);
      r.append(lab, track);
      r.title = `${label}: ${mbpsText(total)} Mbps`;
      bars.append(r);
    };

    for (const [name, v] of entries) {
      const l = byName.get(name);
      const c = l ? seriesColor(l) : cssVar('--muted');
      row(l ? l.label : name, c, [[c, v]], v, false);
    }
    if (res.mixed) {
      const parts = Object.entries(res.mixed_parts || {}).map(([name, v]) => {
        const l = byName.get(name);
        return [l ? seriesColor(l) : cssVar('--muted'), v];
      });
      row('All mixed', null, parts, res.mixed, true);
    }
    const best = Math.max(0, ...entries.map(([, v]) => v));
    foot.hidden = st.running || !entries.length || !res.finished;
    $('stGain').textContent = res.mixed && best > 0
      ? `Mixed is ${(res.mixed / best).toFixed(1)}× your fastest single connection.`
      : 'Weights decide how traffic is shared. Matching them to measured speed is a good start.';
  }

  // ------------------------------------------------------------------ settings card

  function renderUse(s) {
    const sys = $('sysSwitch');
    sys.setAttribute('aria-checked', String(s.sysproxy.enabled));
    sys.disabled = !s.sysproxy.supported;
    $('sysHelp').textContent = s.sysproxy.supported
      ? 'Browsers and most apps follow this. If MixNet closes, they go back to your normal connection.'
      : s.sysproxy.reason;
    $('proxyAddr').textContent = `127.0.0.1:${s.proxy.port}`;
    $('lanSwitch').setAttribute('aria-checked', String(s.proxy.lan_share));
    const lanHelp = $('lanHelp');
    if (s.proxy.lan_share && s.proxy.lan_addresses.length) {
      lanHelp.replaceChildren(document.createTextNode('On your other device, set the HTTP proxy to '));
      s.proxy.lan_addresses.slice(0, 2).forEach((a, i) => {
        if (i) lanHelp.append(document.createTextNode(' or '));
        lanHelp.append(el('code', '', `${a}:${s.proxy.port}`));
      });
      lanHelp.append(document.createTextNode('. Anyone on your network can use it while this is on.'));
    } else {
      lanHelp.textContent = 'Phones, consoles and other computers can use the mix by setting a proxy.';
    }
    $('stickySwitch').setAttribute('aria-checked', String(s.sticky));
  }

  // ------------------------------------------------------------------ activity

  function renderFlows(s) {
    const body = $('flows');
    const byName = new Map(s.links.map((l) => [l.name, l]));
    $('actCount').textContent = s.totals.flows_total ? `${s.totals.flows_total.toLocaleString()} connections since start` : '';
    if (!s.flows.length) return;
    body.replaceChildren();
    for (const f of s.flows) {
      const tr = el('tr', f.open ? '' : 'closed');
      const host = el('td', 'host', f.port === 443 || f.port === 80 ? f.host : `${f.host}:${f.port}`);
      host.title = host.textContent;
      const via = el('td');
      const l = byName.get(f.link);
      if (l) {
        const v = el('span', 'via');
        v.append(swatch(seriesColor(l)), el('span', '', l.label));
        via.append(v);
      } else {
        via.textContent = '—';
      }
      const status = el('td', 'num');
      if (f.error && !f.link) {
        status.append(el('span', 'state-err', 'Failed'));
        status.title = f.error;
      } else if (f.open) {
        status.append(el('span', 'state-open', 'Open'), document.createTextNode(` · ${Math.round(f.age)} s`));
      } else {
        status.textContent = 'Done';
      }
      tr.append(host, via, el('td', 'num', bytes(f.down)), el('td', 'num', bytes(f.up)), status);
      body.append(tr);
    }
  }

  // ------------------------------------------------------------------ wiring

  function setMode(mode) {
    if (state && state.mode !== mode) act('/api/settings', { mode });
  }

  $('modeGroup').addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-mode]');
    if (b) setMode(b.dataset.mode);
  });
  $('modeGroup').addEventListener('keydown', (ev) => {
    const buttons = [...$('modeGroup').querySelectorAll('button')];
    const i = buttons.indexOf(document.activeElement);
    if (i < 0) return;
    let j = null;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') j = (i + 1) % buttons.length;
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') j = (i - 1 + buttons.length) % buttons.length;
    if (j === null) return;
    ev.preventDefault();
    buttons[j].focus();
    setMode(buttons[j].dataset.mode);
  });

  $('sysSwitch').addEventListener('click', () => act('/api/sysproxy', { enabled: !(state && state.sysproxy.enabled) }));
  $('lanSwitch').addEventListener('click', () => act('/api/settings', { lan_share: !(state && state.proxy.lan_share) }));
  $('stickySwitch').addEventListener('click', () => act('/api/settings', { sticky: !(state && state.sticky) }));
  $('stRun').addEventListener('click', () => { lastSpeedKey = ''; act('/api/speedtest', {}); });
  $('stApply').addEventListener('click', async () => {
    await act('/api/speedtest/apply', {});
    toast('Weights updated from the speed test.');
  });
  $('copyBtn').addEventListener('click', async () => {
    const text = $('proxyAddr').textContent;
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied ${text}`);
    } catch {
      const range = document.createRange();
      range.selectNodeContents($('proxyAddr'));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
    }
  });

  const wrap = $('chartWrap');
  wrap.addEventListener('pointermove', onChartPointer);
  wrap.addEventListener('pointerleave', clearHover);

  // Theme: follow the OS unless the viewer picks one.
  try {
    const saved = localStorage.getItem('mixnet-theme');
    if (saved === 'light' || saved === 'dark') root.dataset.theme = saved;
  } catch { /* storage unavailable */ }
  const currentTheme = () => root.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  $('themeBtn').addEventListener('click', () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('mixnet-theme', next); } catch { /* ignore */ }
    lastSpeedKey = '';
    if (state) render(state);
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    lastSpeedKey = '';
    if (state) render(state);
  });
  new ResizeObserver(() => { if (state) drawMain(); }).observe(wrap);

  poll();
})();
