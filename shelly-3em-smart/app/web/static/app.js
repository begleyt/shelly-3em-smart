(() => {
  const cfg = window.APP_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const API = 'api';

  // Toggle state for chart overlays — wired up in boot() to checkboxes.
  const overlayToggles = { ha: true, device: true, shade: true };

  // Cached /api/info response; refreshed periodically. Used by the Insights
  // renderer for currency symbol + rate, and by anything else that needs
  // to know what version of the add-on is talking.
  let appInfo = {};
  async function pollInfo() {
    try {
      const r = await fetch(API + '/info');
      if (r.ok) appInfo = await r.json();
    } catch {}
  }

  // --- Formatters used by Insights ---
  function fmtKwh(wh) {
    if (wh === null || wh === undefined || isNaN(wh)) return '—';
    const kwh = Number(wh) / 1000;
    if (kwh < 0.01) return `${Math.round(Number(wh))} Wh`;
    return `${kwh.toFixed(2)} kWh`;
  }
  function fmtMoney(amount) {
    if (amount === null || amount === undefined || isNaN(amount)) return '—';
    const sym = appInfo.currency_symbol || '$';
    return `${sym}${Number(amount).toFixed(2)}`;
  }
  function fmtDurationLong(seconds) {
    if (!seconds || seconds < 60) return `${Math.round(seconds || 0)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }

  // Stable per-device colors. Hash name → index into a curated categorical
  // palette. Designed to spread hues evenly around the wheel with at most
  // one entry per color family (one green, one teal, one blue, etc.) so
  // even a handful of devices can't all land on the same family.
  const DEVICE_PALETTE = [
    '#e15759', // red
    '#f28e2b', // orange
    '#edc949', // yellow
    '#59a14f', // green
    '#4e79a7', // blue
    '#b07aa1', // purple
    '#d37295', // rose
    '#9c755f', // brown
    '#ff9da7', // peach
    '#76b7b2', // teal
    '#7e57c2', // violet
    '#ffbe7d', // light orange
  ];
  const deviceColorCache = new Map();
  function colorFor(name) {
    if (!deviceColorCache.has(name)) {
      let h = 0;
      for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
      deviceColorCache.set(name, DEVICE_PALETTE[Math.abs(h) % DEVICE_PALETTE.length]);
    }
    return deviceColorCache.get(name);
  }

  // Each plugin pushes hit-test entries here per draw so the canvas mousemove
  // handler can find what the cursor is near.
  const hitTests = new WeakMap();    // chart -> [{ x, ev, type, color }]

  function tsToPixel(buffer, ts, xScale) {
    let bestIdx = -1, bestDiff = Infinity;
    for (let i = 0; i < buffer.length; i++) {
      const diff = Math.abs((buffer[i].ts || 0) - ts);
      if (diff < bestDiff) { bestDiff = diff; bestIdx = i; }
    }
    if (bestIdx < 0 || bestDiff > 60) return null;
    return xScale.getPixelForValue(buffer[bestIdx].label);
  }

  // --- Chart.js plugin: vertical HA-event annotation lines ---
  const haAnnotationPlugin = {
    id: 'haAnnotations',
    afterDatasetsDraw(chart, args, opts) {
      if (!overlayToggles.ha) return;
      const events = (opts && opts.events) || [];
      const buffer = (opts && opts.buffer) || [];
      if (!events.length || !buffer.length) return;
      const { ctx, chartArea: { top, bottom }, scales: { x } } = chart;
      const hits = hitTests.get(chart) || [];

      events.forEach(ev => {
        const px = tsToPixel(buffer, ev.ts, x);
        if (px === null) return;
        const color = ev.direction === 'on' ? '#7fd06b' : '#f72585';
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(px, top);
        ctx.lineTo(px, bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        const name = ev.friendly_name || (ev.entity_id.split('.')[1] || ev.entity_id);
        const short = name.length > 18 ? name.slice(0, 17) + '…' : name;
        ctx.fillStyle = color;
        ctx.font = '10px sans-serif';
        ctx.fillText(`${ev.direction === 'on' ? '▲' : '▼'} ${short}`, px + 3, top + 11);
        ctx.restore();
        hits.push({ x: px, top, bottom, color, type: 'ha', ev });
      });
      hitTests.set(chart, hits);
    },
  };

  // --- Chart.js plugin: device on-periods (shaded) + transition markers ---
  const deviceAnnotationPlugin = {
    id: 'deviceAnnotations',
    afterDatasetsDraw(chart, args, opts) {
      const log = (opts && opts.stateLog) || [];
      const buffer = (opts && opts.buffer) || [];
      if (!log.length || !buffer.length) return;
      const { ctx, chartArea: { top, bottom, left, right }, scales: { x } } = chart;
      const hits = hitTests.get(chart) || [];

      // Group transitions per device id
      const byDevice = new Map();
      for (const ev of log) {
        if (!byDevice.has(ev.device_id)) byDevice.set(ev.device_id, []);
        byDevice.get(ev.device_id).push(ev);
      }

      // Track per-device "row" for stacking transition labels so multiple
      // labels at similar times don't overlap.
      let row = 0;
      for (const [devId, events] of byDevice) {
        const color = colorFor(events[0].device_name);

        // Shaded on-periods: pair consecutive on→off; if still on at end, fill
        // to the right edge of the chart area.
        if (overlayToggles.shade) {
          let onPx = null;
          for (const ev of events) {
            const px = tsToPixel(buffer, ev.ts, x);
            if (px === null) continue;
            if (ev.state === 'on') {
              onPx = px;
            } else if (onPx !== null) {
              ctx.save();
              ctx.fillStyle = color;
              ctx.globalAlpha = 0.10;
              ctx.fillRect(onPx, top, Math.max(1, px - onPx), bottom - top);
              ctx.restore();
              onPx = null;
            }
          }
          if (onPx !== null) {
            ctx.save();
            ctx.fillStyle = color;
            ctx.globalAlpha = 0.10;
            ctx.fillRect(onPx, top, Math.max(1, right - onPx), bottom - top);
            ctx.restore();
          }
        }

        // Transition markers + label rows (only if device-event toggle is on)
        if (overlayToggles.device) {
          const labelY = bottom - 8 - (row % 3) * 12;
          for (const ev of events) {
            const px = tsToPixel(buffer, ev.ts, x);
            if (px === null) continue;
            ctx.save();
            ctx.strokeStyle = color;
            ctx.lineWidth = 1.3;
            ctx.beginPath();
            ctx.moveTo(px, top);
            ctx.lineTo(px, bottom);
            ctx.stroke();
            ctx.restore();
            hits.push({ x: px, top, bottom, color, type: 'device', ev });
          }
          // One label per device near the bottom, anchored to first transition in view
          const firstPx = tsToPixel(buffer, events[0].ts, x);
          if (firstPx !== null) {
            ctx.save();
            ctx.fillStyle = color;
            ctx.font = '10px sans-serif';
            ctx.fillText(events[0].device_name, firstPx + 3, labelY);
            ctx.restore();
          }
        }
        row++;
      }
      hitTests.set(chart, hits);
    },
  };

  // Plugin that resets hit-test array at the start of each draw.
  const hitResetPlugin = {
    id: 'hitReset',
    beforeDatasetsDraw(chart) { hitTests.set(chart, []); },
  };

  if (window.Chart) {
    Chart.register(hitResetPlugin, deviceAnnotationPlugin, haAnnotationPlugin);
  }

  // --- Hover tooltip wiring ---
  function fmtTime(ts) {
    return new Date(ts * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }
  function fmtDuration(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
  }

  function attachChartHover(chart, tooltipEl) {
    const canvas = chart.canvas;
    canvas.addEventListener('mousemove', (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const hits = hitTests.get(chart) || [];
      let nearest = null;
      let minDist = 8;   // px tolerance
      for (const h of hits) {
        if (my < h.top || my > h.bottom) continue;
        const dist = Math.abs(h.x - mx);
        if (dist <= minDist) { minDist = dist; nearest = h; }
      }
      if (!nearest) {
        tooltipEl.classList.add('hidden');
        return;
      }
      const ev = nearest.ev;
      let html = '';
      if (nearest.type === 'ha') {
        const name = ev.friendly_name || ev.entity_id;
        html = `
          <div class="tt-title" style="color:${nearest.color}">
            ${ev.direction === 'on' ? '▲' : '▼'} ${escapeHtml(name)}
          </div>
          <div class="tt-sub">HA entity</div>
          <div>${escapeHtml(ev.old_state || '?')} → ${escapeHtml(ev.new_state || '?')}</div>
          <div class="tt-sub">${fmtTime(ev.ts)}</div>`;
      } else {
        html = `
          <div class="tt-title" style="color:${nearest.color}">
            ${ev.state === 'on' ? '▲' : '▼'} ${escapeHtml(ev.device_name)}
          </div>
          <div class="tt-sub">Detected device · ${ev.state === 'on' ? 'turned on' : 'turned off'}</div>
          <div class="tt-sub">${fmtTime(ev.ts)}</div>`;
      }
      tooltipEl.innerHTML = html;
      tooltipEl.classList.remove('hidden');
      const offsetX = nearest.x + 12;
      const offsetY = Math.max(8, my - 24);
      tooltipEl.style.left = offsetX + 'px';
      tooltipEl.style.top = offsetY + 'px';
    });
    canvas.addEventListener('mouseleave', () => tooltipEl.classList.add('hidden'));
  }

  // --- Tabs ---
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      $('tab-' + btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'insights') loadInsights();
      if (btn.dataset.tab === 'devices') loadDevices();
      if (btn.dataset.tab === 'clusters') loadClusters();
      if (btn.dataset.tab === 'history') loadHistory();
    });
  });

  // --- Live updates ---
  const liveBuf = [];   // [{ts, label, a, b, c, total}, ...]
  let liveChart;
  let liveHaEvents = [];          // recent HA state changes
  let liveDeviceStateLog = [];    // recent device transitions

  function fmt(n, digits = 0) {
    if (n === null || n === undefined) return '—';
    return Number(n).toFixed(digits);
  }

  function initLiveChart() {
    const ctx = $('live-chart').getContext('2d');
    liveChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          { label: cfg.channelA, data: [], borderColor: '#4cc9f0', tension: 0.2, pointRadius: 0 },
          { label: cfg.channelB, data: [], borderColor: '#f72585', tension: 0.2, pointRadius: 0 },
          { label: cfg.channelC, data: [], borderColor: '#7fd06b', tension: 0.2, pointRadius: 0 },
          { label: 'Total', data: [], borderColor: '#e6e9ef', borderWidth: 2, tension: 0.2, pointRadius: 0 },
        ]
      },
      options: {
        responsive: true,
        animation: false,
        scales: {
          y: { ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, title: { display: true, text: 'Watts', color: '#8a93a6' } },
          x: { ticks: { color: '#8a93a6', maxTicksLimit: 8 }, grid: { color: '#2a3340' } }
        },
        plugins: {
          legend: { labels: { color: '#e6e9ef' } },
          haAnnotations: { events: [], buffer: [] },
          deviceAnnotations: { stateLog: [], buffer: [] },
        }
      }
    });
    attachChartHover(liveChart, $('live-tooltip'));
  }

  async function pollOverlays() {
    try {
      const [haR, devR] = await Promise.all([
        fetch(API + '/ha_event_log?minutes=30'),
        fetch(API + '/device_state_log?minutes=30'),
      ]);
      if (haR.ok) liveHaEvents = await haR.json();
      if (devR.ok) liveDeviceStateLog = await devR.json();
    } catch {}
  }

  // Shared latest sample so other tabs (Insights hero) can show live-updating values.
  const state = { latestSample: null };

  async function pollLive() {
    try {
      const r = await fetch(API + '/live');
      if (!r.ok) throw new Error('bad status');
      const s = await r.json();
      state.latestSample = s;
      $('conn-indicator').textContent = 'live';
      $('conn-indicator').className = 'ok';

      // Live tab values
      $('total-power').textContent = Math.round(s.total_power || 0).toLocaleString();
      $('total-current').textContent = fmt(s.total_current, 2);

      // Insights hero — keeps the right-now reading in sync without a full reload
      const heroPower = $('hero-power');
      if (heroPower) heroPower.textContent = Math.round(s.total_power || 0).toLocaleString();
      const heroCurrent = $('hero-current');
      if (heroCurrent) heroCurrent.textContent = (s.total_current || 0).toFixed(1);
      const heroCostRate = $('hero-cost-rate');
      if (heroCostRate && lastInsights) {
        const rate = appInfo.rate_cents_per_kwh || lastInsights.rate_cents_per_kwh || 0;
        if (rate > 0) {
          const cph = ((s.total_power || 0) / 1000) * (rate / 100);
          const sym = appInfo.currency_symbol || lastInsights.currency_symbol || '$';
          heroCostRate.textContent = `${sym}${cph.toFixed(2)}`;
        }
      }
      $('a-power').textContent = Math.round(s.a_power || 0).toLocaleString();
      $('b-power').textContent = Math.round(s.b_power || 0).toLocaleString();
      $('c-power').textContent = Math.round(s.c_power || 0).toLocaleString();
      $('a-current').textContent = fmt(s.a_current, 2);
      $('b-current').textContent = fmt(s.b_current, 2);
      $('c-current').textContent = fmt(s.c_current, 2);
      $('a-voltage').textContent = fmt(s.a_voltage, 1);
      $('b-voltage').textContent = fmt(s.b_voltage, 1);
      $('c-voltage').textContent = fmt(s.c_voltage, 1);
      $('a-pf').textContent = fmt(s.a_pf, 2);
      $('b-pf').textContent = fmt(s.b_pf, 2);
      $('c-pf').textContent = fmt(s.c_pf, 2);

      if (s.ts) {
        const label = new Date(s.ts * 1000).toLocaleTimeString();
        liveBuf.push({ ts: s.ts, label, a: s.a_power, b: s.b_power, c: s.c_power, total: s.total_power });
        if (liveBuf.length > 180) liveBuf.shift();
        liveChart.data.labels = liveBuf.map(p => p.label);
        liveChart.data.datasets[0].data = liveBuf.map(p => p.a);
        liveChart.data.datasets[1].data = liveBuf.map(p => p.b);
        liveChart.data.datasets[2].data = liveBuf.map(p => p.c);
        liveChart.data.datasets[3].data = liveBuf.map(p => p.total);
        // Pass all events through; the plugin's tsToPixel does its own
        // ±60s proximity check against the buffer, so out-of-window events
        // are silently skipped. We avoid hard upper-bound filtering here
        // because a fresh off-event can have ts slightly newer than the
        // most recent buffered sample and would otherwise blink in late.
        const bufView = liveBuf.map(p => ({ ts: p.ts, label: p.label }));
        liveChart.options.plugins.haAnnotations.events = liveHaEvents;
        liveChart.options.plugins.haAnnotations.buffer = bufView;
        liveChart.options.plugins.deviceAnnotations.stateLog = liveDeviceStateLog;
        liveChart.options.plugins.deviceAnnotations.buffer = bufView;
        liveChart.update('none');
      }
    } catch (e) {
      $('conn-indicator').textContent = 'disconnected';
      $('conn-indicator').className = 'warn';
    }
  }

  async function pollStats() {
    try {
      const r = await fetch(API + '/stats');
      const s = await r.json();
      const firstTs = s.first_sample_ts ? new Date(s.first_sample_ts * 1000).toLocaleString() : '—';
      $('stats').innerHTML = `
        <div><span>Samples</span><span>${s.samples.toLocaleString()}</span></div>
        <div><span>Events</span><span>${s.events.toLocaleString()}</span></div>
        <div><span>Devices</span><span>${s.devices}</span></div>
        <div><span>Unlabeled clusters</span><span>${s.unlabeled_clusters}</span></div>
        <div><span>Recording since</span><span>${firstTs}</span></div>
      `;
    } catch {}
  }

  // --- Insights ---
  async function loadInsights() {
    try {
      const [insR, histR] = await Promise.all([
        fetch(API + '/insights'),
        fetch(API + '/history_summary'),
      ]);
      if (insR.ok) {
        renderInsights(await insR.json());
      }
      if (histR.ok) {
        renderHistory(await histR.json());
      }
    } catch (e) {
      console.warn('insights fetch failed', e);
    }
  }

  function renderHistory(hist) {
    const rate = appInfo.rate_cents_per_kwh || hist.rate_cents_per_kwh || 0;
    const hasRate = rate > 0;
    const sym = appInfo.currency_symbol || hist.currency_symbol || '$';
    const setCell = (kwhId, costId, bucket) => {
      const wh = (bucket && bucket.wh) || 0;
      const cost = (bucket && bucket.cost) || 0;
      const kwhEl = $(kwhId);
      const costEl = $(costId);
      if (kwhEl) kwhEl.textContent = fmtKwh(wh);
      if (costEl) costEl.textContent = hasRate ? `${sym}${cost.toFixed(2)}` : '';
    };
    setCell('hist-today-kwh',     'hist-today-cost',     hist.today);
    setCell('hist-yesterday-kwh', 'hist-yesterday-cost', hist.yesterday);
    setCell('hist-7d-kwh',        'hist-7d-cost',        hist.last_7d);
    setCell('hist-30d-kwh',       'hist-30d-cost',       hist.last_30d);
  }

  let energyDonutChart = null;
  let lastInsights = null;

  function renderInsights(ins) {
    lastInsights = ins;
    const panel = ins.panel_today || {};
    const phantom = ins.phantom_load || {};
    const attributed = ins.attributed_wh || 0;
    const rate = appInfo.rate_cents_per_kwh || ins.rate_cents_per_kwh || 0;
    const hasRate = rate > 0;
    const sym = appInfo.currency_symbol || ins.currency_symbol || '$';

    // -- Hero: live values driven by pollLive too, but seed from insights data
    const livePower = (state.latestSample && state.latestSample.total_power) || 0;
    $('hero-power').textContent = Math.round(livePower).toLocaleString();
    $('hero-current').textContent = ((state.latestSample && state.latestSample.total_current) || 0).toFixed(1);
    const devicesOn = (ins.all_devices_today || []).filter(d => d.is_on).length;
    $('hero-devices-on').textContent = devicesOn;
    if (hasRate) {
      // Right-now cost rate per hour at current power draw
      const costPerHour = (livePower / 1000) * (rate / 100);
      $('hero-cost-rate').textContent = `${sym}${costPerHour.toFixed(2)}`;
      $('hero-cost-sub').textContent = '/hour at current draw';
    } else {
      $('hero-cost-rate').textContent = '—';
      $('hero-cost-sub').textContent = 'Set rate in add-on options';
    }

    // -- 4 stat cards
    $('stat-today-energy').innerHTML = fmtKwh(panel.wh);
    $('stat-today-cost').textContent = hasRate ? fmtMoney(panel.cost) : 'set rate to see cost';

    $('stat-phantom').innerHTML = `${Math.round(phantom.watts || 0)}<span class="insight-headline-unit">W</span>`;
    $('stat-phantom-cost').textContent = hasRate ? `${fmtMoney(phantom.daily_cost)}/day` : `${fmtKwh(phantom.daily_wh)}/day`;

    const attrPct = panel.wh ? Math.round((attributed / panel.wh) * 100) : 0;
    $('stat-attribution').innerHTML = `${attrPct}<span class="insight-headline-unit">%</span>`;
    $('stat-attribution-detail').textContent = `${fmtKwh(attributed)} of ${fmtKwh(panel.wh)}`;

    const anomalyCount = (ins.anomalies || []).length;
    $('stat-anomaly-count').textContent = anomalyCount;
    $('stat-anomaly-status').textContent = anomalyCount ? 'see below' : 'all normal';

    // -- Donut: today's energy by device
    renderEnergyDonut(ins);

    // -- Activity feed: merge HA + device events, sort newest first
    renderActivityFeed(ins);

    // -- Top consumers
    const top = ins.top_devices_today || [];
    const topMax = Math.max(1, ...top.map(d => d.energy_wh || 0));
    if (!top.length) {
      $('insights-top').innerHTML = '<div class="empty">No labelled devices have used energy today yet.</div>';
    } else {
      $('insights-top').innerHTML = top.map(d => {
        const pct = Math.max(2, Math.round(((d.energy_wh || 0) / topMax) * 100));
        return `
          <div class="device-stat-row">
            <div class="device-state ${d.is_on ? 'on' : ''}">${d.is_on ? 'ON' : 'OFF'}</div>
            <div>
              <div style="font-weight:600;color:var(--fg-1);">${escapeHtml(d.name)}</div>
              <div class="stat-bar"><div class="stat-bar-fill" style="width:${pct}%;background:${colorFor(d.name)};"></div></div>
            </div>
            <div class="stat-pill">${d.cycles_today || 0} cycles</div>
            <div class="stat-pill">${fmtDurationLong(d.runtime_seconds || 0)}</div>
            <div style="text-align:right;min-width:90px;">
              <div style="font-weight:600;font-variant-numeric:tabular-nums;color:var(--fg-1);">${fmtKwh(d.energy_wh || 0)}</div>
              ${hasRate ? `<div class="card-sub">${fmtMoney(d.cost || 0)}</div>` : ''}
            </div>
          </div>`;
      }).join('');
    }

    // -- Anomalies
    if (anomalyCount) {
      $('insights-anomalies-card').style.display = '';
      $('insights-anomalies').innerHTML = ins.anomalies.map(a => `
        <div class="anomaly-row">
          <span><b>${escapeHtml(a.name)}</b></span>
          <span class="muted">${escapeHtml(a.anomaly || '')}</span>
        </div>
      `).join('');
    } else {
      $('insights-anomalies-card').style.display = 'none';
    }
  }

  function renderEnergyDonut(ins) {
    const devices = (ins.all_devices_today || []).filter(d => (d.energy_wh || 0) > 0);
    const panelWh = (ins.panel_today && ins.panel_today.wh) || 0;
    const attributedWh = ins.attributed_wh || 0;
    const unattributedWh = Math.max(0, panelWh - attributedWh);

    devices.sort((a, b) => (b.energy_wh || 0) - (a.energy_wh || 0));
    const slices = devices.map(d => ({
      label: d.name,
      wh: d.energy_wh || 0,
      color: colorFor(d.name),
    }));
    if (unattributedWh > 0.001) {
      slices.push({ label: 'Unattributed', wh: unattributedWh, color: '#3a4250' });
    }

    const total = slices.reduce((s, x) => s + x.wh, 0);
    const canvas = $('energy-donut');
    if (!canvas) return;
    if (!total || !slices.length) {
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#3a4250';
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('No energy yet today', canvas.width / 2, canvas.height / 2);
      $('donut-legend').innerHTML = '';
      return;
    }

    if (energyDonutChart) energyDonutChart.destroy();
    energyDonutChart = new Chart(canvas, {
      type: 'doughnut',
      data: {
        labels: slices.map(s => s.label),
        datasets: [{
          data: slices.map(s => s.wh),
          backgroundColor: slices.map(s => s.color),
          borderColor: '#0a0e13',
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,  // square wrapper drives both dimensions
        plugins: { legend: { display: false }, tooltip: {
          callbacks: { label: ctx => `${ctx.label}: ${fmtKwh(ctx.parsed)} (${((ctx.parsed/total)*100).toFixed(1)}%)` }
        }},
        cutout: '64%',
        animation: { duration: 400 },
      },
    });

    $('donut-legend').innerHTML = slices.map(s => {
      const pct = ((s.wh / total) * 100).toFixed(1);
      return `
        <div class="donut-legend-row">
          <span class="donut-legend-swatch" style="background:${s.color};"></span>
          <span>${escapeHtml(s.label)}</span>
          <span class="pct">${fmtKwh(s.wh)} · ${pct}%</span>
        </div>`;
    }).join('');
  }

  async function renderActivityFeed(ins) {
    // Pull recent HA + device events. Both endpoints already exist.
    try {
      const [haR, dvR] = await Promise.all([
        fetch(API + '/ha_event_log?minutes=720&limit=80'),
        fetch(API + '/device_state_log?minutes=720&limit=80'),
      ]);
      const ha = haR.ok ? await haR.json() : [];
      const dv = dvR.ok ? await dvR.json() : [];
      const items = [];
      for (const e of ha) {
        items.push({ ts: e.ts, name: e.friendly_name || e.entity_id, direction: e.direction, source: 'HA' });
      }
      for (const e of dv) {
        items.push({ ts: e.ts, name: e.device_name, direction: e.state, source: 'Detected' });
      }
      items.sort((a, b) => b.ts - a.ts);
      const root = $('activity-feed');
      if (!items.length) {
        root.innerHTML = '<div class="empty" style="padding:18px;">No recent events.</div>';
        return;
      }
      root.innerHTML = items.slice(0, 30).map(it => {
        const cls = it.direction === 'on' ? 'on' : 'off';
        const arrow = it.direction === 'on' ? '▲ ON' : '▼ OFF';
        return `
          <div class="activity-row ${cls}">
            <span class="arrow">${arrow}</span>
            <span class="name">${escapeHtml(it.name)} <span class="src">${it.source}</span></span>
            <span class="time">${fmtTimeShort(it.ts)}</span>
          </div>`;
      }).join('');
    } catch {}
  }

  function fmtTimeShort(ts) {
    const d = new Date(ts * 1000);
    const now = Date.now();
    const ageMs = now - d.getTime();
    if (ageMs < 60000)   return Math.round(ageMs / 1000) + 's ago';
    if (ageMs < 3600000) return Math.round(ageMs / 60000) + 'm ago';
    const todayStart = new Date(); todayStart.setHours(0,0,0,0);
    if (d.getTime() >= todayStart.getTime()) {
      return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  // --- Devices ---
  async function loadDevices() {
    // Pull devices and today's stats together so we can render rich rows
    // without N round-trips. Insights endpoint already computes all_devices_today.
    const [devR, insR] = await Promise.all([
      fetch(API + '/devices'),
      fetch(API + '/insights'),
    ]);
    const devices = await devR.json();
    const insights = insR.ok ? await insR.json() : { all_devices_today: [], rate_cents_per_kwh: 0 };
    const statsById = new Map();
    for (const s of (insights.all_devices_today || [])) statsById.set(s.device_id, s);
    const anomaliesById = new Map();
    for (const a of (insights.anomalies || [])) anomaliesById.set(a.device_id, a.anomaly);
    const hasRate = (insights.rate_cents_per_kwh || appInfo.rate_cents_per_kwh || 0) > 0;
    const totalToday = (insights.attributed_wh || 0) || 1;

    const root = $('devices-list');
    if (!devices.length) {
      root.innerHTML = `<div class="empty">No devices yet. Label a cluster to start tracking one.</div>`;
      return;
    }

    root.innerHTML = devices.map(d => {
      const stats = statsById.get(d.id) || {};
      const anomaly = anomaliesById.get(d.id);
      const share = ((stats.energy_wh || 0) / totalToday) * 100;
      const tags = [];
      const isMetered = (stats.energy_source === 'metered') || (d.energy_source === 'metered');
      if (isMetered) tags.push('<span class="tag metered" title="Energy comes directly from an HA sensor, not inferred">⚡ Metered</span>');
      if (d.is_continuous) tags.push('<span class="tag continuous">Continuous</span>');
      if (d.source_entity_id) tags.push(`<span class="tag via">via ${escapeHtml(d.source_entity_id)}</span>`);
      if (anomaly) tags.push(`<span class="tag anomaly" title="${escapeHtml(anomaly)}">⚠ Anomaly</span>`);

      const energyHtml = (stats.energy_wh || 0) > 0
        ? `<div style="font-weight:600;font-variant-numeric:tabular-nums;">${fmtKwh(stats.energy_wh)}</div>
           ${hasRate ? `<div class="card-sub">${fmtMoney(stats.cost || 0)}</div>` : ''}`
        : '<div class="card-sub">no energy yet</div>';

      return `
      <div class="list-item" style="grid-template-columns:auto 1fr auto auto auto auto;">
        <div class="device-state ${d.is_on ? 'on' : ''}">${d.is_on ? 'ON' : 'OFF'}</div>
        <div>
          <div class="name">${escapeHtml(d.name)} ${tags.join(' ')}</div>
          <div class="meta">
            <span>${Math.round(d.mean_power_w || 0)} W</span>
            ${d.notes ? `<span>· ${escapeHtml(d.notes)}</span>` : ''}
            <span>· ${d.last_on_ts ? 'last on ' + new Date(d.last_on_ts * 1000).toLocaleString() : 'never seen on'}</span>
          </div>
          <div class="stat-bar"><div class="stat-bar-fill" style="width:${Math.min(100, Math.max(0, share))}%;"></div></div>
        </div>
        <div style="text-align:center;min-width:60px;">
          <div style="font-weight:600;font-variant-numeric:tabular-nums;">${stats.cycles_today || 0}</div>
          <div class="card-sub">cycles</div>
        </div>
        <div style="text-align:center;min-width:80px;">
          <div style="font-weight:600;font-variant-numeric:tabular-nums;">${fmtDurationLong(stats.runtime_seconds || 0)}</div>
          <div class="card-sub">runtime</div>
        </div>
        <div style="text-align:right;min-width:90px;">${energyHtml}</div>
        <div style="display:flex;gap:4px;">
          <button data-edit='${JSON.stringify(d).replace(/'/g, "&#39;")}'>Edit</button>
          <button class="danger" data-del="${d.id}">Delete</button>
        </div>
      </div>`;
    }).join('');

    root.querySelectorAll('[data-del]').forEach(b => {
      b.addEventListener('click', async () => {
        if (!confirm('Delete this device? The cluster will become unlabeled again.')) return;
        await fetch(API + '/devices/' + b.dataset.del, { method: 'DELETE' });
        loadDevices();
      });
    });
    root.querySelectorAll('[data-edit]').forEach(b => {
      b.addEventListener('click', () => {
        const d = JSON.parse(b.dataset.edit.replace(/&#39;/g, "'"));
        openEditModal(d);
      });
    });
  }

  // --- Clusters ---
  let lastClusters = []; // flat list of all clusters, for modal lookup

  async function loadClusters() {
    const r = await fetch(API + '/cluster_pairs');
    const { pairs, orphans } = await r.json();
    const root = $('clusters-list');

    lastClusters = [];
    pairs.forEach(p => { lastClusters.push(p.on_cluster, p.off_cluster); });
    orphans.forEach(o => { lastClusters.push(o.cluster); });

    if (!pairs.length && !orphans.length) {
      root.innerHTML = `<div class="empty">No unlabeled clusters yet. The system needs ~5 similar events to form one. Be patient — the longer it runs, the better.</div>`;
      return;
    }

    const pairRows = pairs.map(p => {
      const on = p.on_cluster, off = p.off_cluster;
      const w = Math.round(p.mean_power_w);
      const startTimes = formatRecentTimes(on.recent_event_ts);
      const stopTimes = formatRecentTimes(off.recent_event_ts);
      return `
      <div class="list-item">
        <div class="dir on" title="Auto-paired start + stop clusters. Labelling here links both to one device.">Pair</div>
        <div>
          <div class="name">~${w} W appliance <span class="muted">(${p.total_events} events)</span></div>
          <div class="meta">
            <span>${cfg.channelA}: ${Math.round((on.mean_a_power - off.mean_a_power)/2)} W</span>
            <span>${cfg.channelB}: ${Math.round((on.mean_b_power - off.mean_b_power)/2)} W</span>
            <span>${cfg.channelC}: ${Math.round((on.mean_c_power - off.mean_c_power)/2)} W</span>
            <span>pf ${Number(on.mean_pf || 0).toFixed(2)}</span>
            <span>start: ${on.sample_count}</span>
            <span>stop: ${off.sample_count}</span>
          </div>
          <div class="meta" style="margin-top:4px;font-size:0.72rem;">
            <span>Recent starts: ${startTimes || '—'}</span>
            <span>Recent stops: ${stopTimes || '—'}</span>
          </div>
        </div>
        <button class="primary" data-label="${on.id}">Label</button>
      </div>`;
    }).join('');

    const orphanRows = orphans.map(o => {
      const c = o.cluster;
      const label = c.mean_power > 0 ? 'Start' : 'Stop';
      const dir = c.mean_power > 0 ? 'on' : 'off';
      const times = formatRecentTimes(c.recent_event_ts);
      return `
      <div class="list-item">
        <div class="dir ${dir}" title="Couldn't find a confident pair. Labelling will still work but the matcher only catches one direction.">${label}</div>
        <div>
          <div class="name">~${Math.abs(Math.round(c.mean_power))} W <span class="muted">(± ${Math.round(c.std_power)})</span></div>
          <div class="meta">
            <span>${cfg.channelA}: ${Math.round(c.mean_a_power)} W</span>
            <span>${cfg.channelB}: ${Math.round(c.mean_b_power)} W</span>
            <span>${cfg.channelC}: ${Math.round(c.mean_c_power)} W</span>
            <span>pf ${Number(c.mean_pf || 0).toFixed(2)}</span>
            <span>${c.sample_count} events</span>
          </div>
          <div class="meta" style="margin-top:4px;font-size:0.72rem;">
            <span>Recent: ${times || '—'}</span>
          </div>
        </div>
        <button class="primary" data-label="${c.id}">Label</button>
      </div>`;
    }).join('');

    const pairHeader = pairs.length ? '<h3 style="margin:8px 0 4px;font-size:0.9rem;color:var(--fg-dim);">Probable appliances (start + stop paired)</h3>' : '';
    const orphanHeader = orphans.length ? '<h3 style="margin:16px 0 4px;font-size:0.9rem;color:var(--fg-dim);">Unpaired clusters</h3>' : '';

    root.innerHTML = pairHeader + pairRows + orphanHeader + orphanRows;
    root.querySelectorAll('[data-label]').forEach(b => {
      b.addEventListener('click', () => openLabelModal(b.dataset.label, lastClusters.find(c => c.id == b.dataset.label)));
    });
  }

  $('recluster-btn').addEventListener('click', async () => {
    $('recluster-status').textContent = 'running…';
    const r = await fetch(API + '/recluster', { method: 'POST' });
    const j = await r.json();
    $('recluster-status').textContent = `found ${j.on} on-clusters, ${j.off} off-clusters · absorbed ${j.absorbed || 0} into existing devices`;
    loadClusters();
    loadDevices();
  });

  $('absorb-btn').addEventListener('click', async () => {
    $('recluster-status').textContent = 'absorbing…';
    const r = await fetch(API + '/absorb_clusters', { method: 'POST' });
    const j = await r.json();
    $('recluster-status').textContent = `absorbed ${j.absorbed} clusters (${j.linked_events} events) into existing devices`;
    loadClusters();
    loadDevices();
  });

  // --- Label modal ---
  let modalClusterId = null;
  function openLabelModal(clusterId, cluster) {
    modalClusterId = clusterId;
    $('modal-cluster-info').textContent = cluster
      ? `Cluster #${cluster.id}: ~${Math.abs(Math.round(cluster.mean_power))} W, ${cluster.sample_count} matching events`
      : '';
    $('modal-name').value = '';
    $('modal-notes').value = '';
    $('modal').classList.remove('hidden');
    $('modal-name').focus();
  }
  $('modal-cancel').addEventListener('click', () => $('modal').classList.add('hidden'));
  $('modal-save').addEventListener('click', async () => {
    const name = $('modal-name').value.trim();
    if (!name) { alert('Name required'); return; }
    const notes = $('modal-notes').value.trim() || null;
    const r = await fetch(API + '/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, notes, cluster_id: parseInt(modalClusterId) })
    });
    if (r.ok) {
      $('modal').classList.add('hidden');
      loadClusters();
      loadDevices();
    } else {
      alert('Failed to save device');
    }
  });

  // --- Manual add modal ---
  function openManualModal() {
    ['manual-name', 'manual-power', 'manual-power-a', 'manual-power-b', 'manual-power-c', 'manual-notes']
      .forEach(id => { $(id).value = ''; });
    $('manual-currently-on').checked = false;
    $('manual-continuous').checked = false;
    $('modal-manual').classList.remove('hidden');
    $('manual-name').focus();
  }

  // --- Edit device modal ---
  let editingDeviceId = null;
  function openEditModal(d) {
    editingDeviceId = d.id;
    $('edit-device-info').textContent = `${d.name} · ~${Math.round(d.mean_power_w || 0)} W · currently ${d.is_on ? 'ON' : 'OFF'}`;
    $('edit-name').value = d.name || '';
    $('edit-notes').value = d.notes || '';
    $('edit-continuous').checked = !!d.is_continuous;
    $('modal-edit').classList.remove('hidden');
    $('edit-name').focus();
  }
  $('edit-cancel').addEventListener('click', () => $('modal-edit').classList.add('hidden'));
  $('edit-save').addEventListener('click', async () => {
    if (editingDeviceId == null) return;
    const body = {
      name: $('edit-name').value.trim() || null,
      notes: $('edit-notes').value.trim() || null,
      is_continuous: $('edit-continuous').checked,
    };
    const r = await fetch(API + '/devices/' + editingDeviceId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      $('modal-edit').classList.add('hidden');
      loadDevices();
    } else {
      alert('Failed to save');
    }
  });
  $('edit-force-on').addEventListener('click', async () => {
    if (editingDeviceId == null) return;
    await fetch(API + '/devices/' + editingDeviceId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force_state: 'on' }),
    });
    $('modal-edit').classList.add('hidden');
    loadDevices();
  });
  $('edit-force-off').addEventListener('click', async () => {
    if (editingDeviceId == null) return;
    await fetch(API + '/devices/' + editingDeviceId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force_state: 'off' }),
    });
    $('modal-edit').classList.add('hidden');
    loadDevices();
  });
  $('manual-add-btn').addEventListener('click', openManualModal);
  $('manual-cancel').addEventListener('click', () => $('modal-manual').classList.add('hidden'));
  $('manual-save').addEventListener('click', async () => {
    const name = $('manual-name').value.trim();
    const powerW = parseFloat($('manual-power').value);
    if (!name) { alert('Name required'); return; }
    if (!powerW || powerW <= 0) { alert('Total power (W) must be positive'); return; }
    const num = (id) => {
      const v = $(id).value.trim();
      return v === '' ? null : parseFloat(v);
    };
    const body = {
      name,
      notes: $('manual-notes').value.trim() || null,
      power_w: powerW,
      channel_a_power_w: num('manual-power-a'),
      channel_b_power_w: num('manual-power-b'),
      channel_c_power_w: num('manual-power-c'),
      currently_on: $('manual-currently-on').checked,
      is_continuous: $('manual-continuous').checked,
    };
    const r = await fetch(API + '/devices/manual', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (r.ok) {
      const result = await r.json();
      $('modal-manual').classList.add('hidden');
      loadDevices();
      if (result.matched_history_events) {
        // Quick toast-ish indicator in the recluster-status row (cheap reuse).
        console.log(`Linked ${result.matched_history_events} historic events to ${result.name}`);
      }
    } else {
      const err = await r.json().catch(() => ({}));
      alert('Failed to create device: ' + (err.detail || 'unknown error'));
    }
  });

  // --- History ---
  let historyChart;
  async function loadHistory() {
    const minutes = parseInt($('history-window').value);
    const [historyResp, eventsResp, devResp] = await Promise.all([
      fetch(API + '/history?minutes=' + minutes),
      fetch(API + '/ha_event_log?minutes=' + minutes + '&limit=500'),
      fetch(API + '/device_state_log?minutes=' + minutes + '&limit=2000'),
    ]);
    const data = await historyResp.json();
    const events = eventsResp.ok ? await eventsResp.json() : [];
    const stateLog = devResp.ok ? await devResp.json() : [];
    const labels = data.map(d => new Date(d.ts * 1000).toLocaleTimeString());
    const buffer = data.map((d, i) => ({ ts: d.ts, label: labels[i] }));
    const cfg2 = {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: cfg.channelA, data: data.map(d => d.a_power), borderColor: '#4cc9f0', tension: 0.2, pointRadius: 0 },
          { label: cfg.channelB, data: data.map(d => d.b_power), borderColor: '#f72585', tension: 0.2, pointRadius: 0 },
          { label: cfg.channelC, data: data.map(d => d.c_power), borderColor: '#7fd06b', tension: 0.2, pointRadius: 0 },
          { label: 'Total', data: data.map(d => d.total_power), borderColor: '#e6e9ef', borderWidth: 2, tension: 0.2, pointRadius: 0 },
        ]
      },
      options: {
        responsive: true,
        animation: false,
        scales: {
          y: { ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, title: { display: true, text: 'Watts', color: '#8a93a6' } },
          x: { ticks: { color: '#8a93a6', maxTicksLimit: 12 }, grid: { color: '#2a3340' } }
        },
        plugins: {
          legend: { labels: { color: '#e6e9ef' } },
          haAnnotations: { events, buffer },
          deviceAnnotations: { stateLog, buffer },
        }
      }
    };
    if (historyChart) historyChart.destroy();
    historyChart = new Chart($('history-chart'), cfg2);
    attachChartHover(historyChart, $('history-tooltip'));
  }
  $('history-window').addEventListener('change', loadHistory);

  // --- utils ---
  function formatRecentTimes(timestamps) {
    if (!timestamps || !timestamps.length) return '';
    const now = Date.now();
    const todayStart = new Date(); todayStart.setHours(0, 0, 0, 0);
    return timestamps.slice(0, 4).map(ts => {
      const d = new Date(ts * 1000);
      const ageMs = now - d.getTime();
      // Short relative form for very recent events
      if (ageMs < 60000) return Math.round(ageMs / 1000) + 's ago';
      if (ageMs < 3600000) return Math.round(ageMs / 60000) + 'm ago';
      // Absolute time for today, day+time for older
      if (d.getTime() >= todayStart.getTime()) {
        return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      }
      return d.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    }).join(', ');
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[c]);
  }

  // --- Overlay toggle wiring ---
  function refreshChart(chart) { if (chart) chart.update('none'); }
  $('toggle-ha-overlay').addEventListener('change', e => {
    overlayToggles.ha = e.target.checked; refreshChart(liveChart); refreshChart(historyChart);
  });
  $('toggle-device-overlay').addEventListener('change', e => {
    overlayToggles.device = e.target.checked; refreshChart(liveChart); refreshChart(historyChart);
  });
  $('toggle-device-shade').addEventListener('change', e => {
    overlayToggles.shade = e.target.checked; refreshChart(liveChart); refreshChart(historyChart);
  });

  // --- boot ---
  initLiveChart();
  pollInfo();
  pollLive();
  pollStats();
  pollOverlays();
  loadInsights();
  setInterval(pollLive, 1500);
  setInterval(pollStats, 15000);
  setInterval(pollOverlays, 2000);
  setInterval(loadInsights, 15000);
  setInterval(pollInfo, 60000);
  setInterval(() => {
    if (document.querySelector('.tab.active').dataset.tab === 'devices') loadDevices();
  }, 5000);
})();
