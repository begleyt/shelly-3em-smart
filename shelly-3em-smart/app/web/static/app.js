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
      if (btn.dataset.tab === 'climate') loadClimate();
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
      const [insR, histR, fcR] = await Promise.all([
        fetch(API + '/insights'),
        fetch(API + '/history_summary'),
        fetch(API + '/forecast/energy?days_ahead=7'),
      ]);
      if (insR.ok) {
        renderInsights(await insR.json());
      }
      if (histR.ok) {
        renderHistory(await histR.json());
      }
      if (fcR.ok) {
        renderForecast(await fcR.json());
      }
    } catch (e) {
      console.warn('insights fetch failed', e);
    }
  }

  let forecastChart = null;
  function renderForecast(fc) {
    const card = $('forecast-card');
    if (!fc || !fc.has_forecast || !fc.days || !fc.days.length) {
      card.style.display = 'none';
      return;
    }
    card.style.display = '';
    const sym = (appInfo && appInfo.currency_symbol) || fc.currency_symbol || '$';
    const rate = (appInfo && appInfo.rate_cents_per_kwh) || fc.rate_cents_per_kwh || 0;
    const hasRate = rate > 0;
    const unit = (appInfo && appInfo.temp_unit) || fc.temp_unit || 'F';

    // Tomorrow stats
    const tomorrow = fc.days[0];
    if (tomorrow) {
      $('forecast-tomorrow-kwh').textContent = `${tomorrow.predicted_kwh.toFixed(1)} kWh`;
      $('forecast-tomorrow-cost').textContent = hasRate ? `${sym}${tomorrow.predicted_cost.toFixed(2)}` : '';
      const high = tomorrow.forecast_high_f;
      const low = tomorrow.forecast_low_f;
      if (high !== null && low !== null) {
        $('forecast-tomorrow-temp').textContent =
          `${fmtTemp(high, unit)} / ${fmtTemp(low, unit)} · ${tomorrow.condition || ''}`;
      } else {
        $('forecast-tomorrow-temp').textContent = '';
      }
    }

    // Week stats
    $('forecast-week-kwh').textContent = `${fc.total_kwh.toFixed(1)} kWh`;
    $('forecast-week-cost').textContent = hasRate ? `${sym}${fc.total_cost.toFixed(2)}` : '';
    $('forecast-week-meta').textContent = `${fc.days.length} days`;

    // Chart: bars per day for predicted kWh
    const canvas = $('forecast-chart');
    if (!canvas) return;
    const labels = fc.days.map(d => d.date_str.slice(5));
    const kwh = fc.days.map(d => d.predicted_kwh);
    if (forecastChart) forecastChart.destroy();
    forecastChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Predicted kWh',
          data: kwh,
          backgroundColor: '#4e79a7',
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: ctx => {
              const d = fc.days[ctx.dataIndex];
              const lines = [`${d.predicted_kwh.toFixed(1)} kWh`];
              if (hasRate) lines.push(`${sym}${d.predicted_cost.toFixed(2)}`);
              if (d.forecast_high_f !== null) {
                lines.push(`${fmtTemp(d.forecast_high_f, unit)} / ${fmtTemp(d.forecast_low_f, unit)}`);
              }
              return lines.join(' · ');
            },
          }},
        },
        scales: {
          x: { ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' } },
          y: { title: { display: true, text: 'kWh', color: '#8a93a6' },
               ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, beginAtZero: true },
        },
      },
    });

    // Method note
    let methodTxt;
    if (fc.model_r_squared !== null) {
      methodTxt = `Predictions from HDD/CDD regression (R² ${fc.model_r_squared.toFixed(2)}, n=${fc.model_n}) on the last 30 days, applied to your weather entity's daily forecast.`;
    } else {
      const src = fc.days[0] && fc.days[0].source;
      methodTxt = src === 'recent_average'
        ? 'Using the last 30-day average kWh — fitted regression will activate once there are 5+ completed days with temperature data.'
        : 'No model yet — gathering data.';
    }
    $('forecast-method-note').textContent = methodTxt;
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

  // --- Climate / weather tab ---
  let climateScatterChart = null;
  let climateDegreeBarsChart = null;

  function fmtTemp(f, unitPref) {
    if (f === null || f === undefined) return '—';
    const useC = (unitPref === 'C');
    const v = useC ? ((f - 32) * 5/9) : f;
    return `${v.toFixed(1)}°${useC ? 'C' : 'F'}`;
  }

  function fmtDD(dd) {
    if (dd === null || dd === undefined) return '0';
    return Number(dd).toFixed(1);
  }

  async function loadClimate() {
    const supportsWeather = appInfo && appInfo.supports_weather;
    const unit = (appInfo && appInfo.temp_unit) || 'F';
    const baseF = (appInfo && appInfo.hdd_cdd_base_temp_f) || 65.0;
    $('climate-base-temp').textContent = unit === 'C'
      ? `${((baseF - 32) * 5/9).toFixed(1)}°C`
      : `${baseF.toFixed(0)}°F`;

    // Show banner if no weather entity configured server-side
    if (!supportsWeather || !appInfo.weather_entity_id) {
      $('climate-banner').style.display = '';
    } else {
      $('climate-banner').style.display = 'none';
    }

    try {
      const [nowR, rollR, anomR, devR, savR, entR, boundsR] = await Promise.all([
        fetch(API + '/weather/now'),
        fetch(API + '/daily_rollups?days=30'),
        fetch(API + '/weather/anomaly'),
        fetch(API + '/devices'),
        fetch(API + '/setpoint/savings'),
        fetch(API + '/setpoint/entities'),
        fetch(API + '/setpoint/bounds'),
      ]);
      const now = nowR.ok ? await nowR.json() : null;
      const roll = rollR.ok ? await rollR.json() : { days: [] };
      const anom = anomR.ok ? await anomR.json() : null;
      const devs = devR.ok ? await devR.json() : [];
      const savings = savR.ok ? await savR.json() : null;
      const ents = entR.ok ? await entR.json() : { entities: [] };
      const bounds = boundsR.ok ? await boundsR.json() : null;

      renderClimateNow(now, unit);
      renderClimateAnomaly(anom);
      renderClimateScatter(roll.days || [], unit);
      renderClimateDegreeBars(roll.days || [], unit);
      renderCoolingSection(now, anom, roll.days || [], devs, unit);
      renderHeatingSection(now, anom, roll.days || [], devs, unit);
      renderSetpointTimeline(roll.days || [], unit);
      renderSetpointControl(ents.entities || [], savings, bounds, unit);
    } catch (e) {
      console.warn('climate fetch failed', e);
    }
  }

  // --- Setpoint control: round Nest-style dial + savings preview ---
  let currentEntities = [];
  let currentSavings = null;
  let savingsBounds = null;
  let savingsUnit = 'F';
  let originalCool = null, originalHeat = null;
  let desiredCoolF = null, desiredHeatF = null;
  let dialMode = 'cool';   // 'cool' | 'heat' — which setpoint the dial controls

  // Dial geometry
  const DIAL_CX = 160, DIAL_CY = 160, DIAL_R = 140;
  // Sweep across the top 270° of the circle (135°→405° in standard math
  // coords). We use SVG angle space here (0° = +x, CCW positive).
  const DIAL_START_DEG = -210;     // bottom-left
  const DIAL_END_DEG   =   30;     // bottom-right (240° sweep)

  function _fmtMoney(amount, currency = '$') {
    if (amount === null || amount === undefined) return '—';
    const sign = amount >= 0 ? '+' : '−';
    return `${sign}${currency}${Math.abs(amount).toFixed(2)}`;
  }

  // Convert temperature to angle around the dial. Linear interpolation
  // between the bounds for the active mode.
  function _tempToAngle(t) {
    const range = _activeBounds();
    if (!range) return DIAL_START_DEG;
    const frac = Math.max(0, Math.min(1, (t - range.min) / (range.max - range.min)));
    return DIAL_START_DEG + frac * (DIAL_END_DEG - DIAL_START_DEG);
  }
  function _angleToTemp(angleDeg) {
    const range = _activeBounds();
    if (!range) return null;
    const frac = (angleDeg - DIAL_START_DEG) / (DIAL_END_DEG - DIAL_START_DEG);
    return range.min + Math.max(0, Math.min(1, frac)) * (range.max - range.min);
  }
  function _activeBounds() {
    if (!savingsBounds) return null;
    return dialMode === 'cool'
      ? { min: savingsBounds.cool_min_f, max: savingsBounds.cool_max_f }
      : { min: savingsBounds.heat_min_f, max: savingsBounds.heat_max_f };
  }
  function _polar(cx, cy, r, angleDeg) {
    const rad = angleDeg * Math.PI / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
  }

  // Extrapolate beyond the server's computed deltas. Server only returns
  // scenarios at [-2,-1,+1,+2] so we interp/extrap to get smooth dial feel.
  function _approxScenario(direction, deltaF) {
    if (!currentSavings || !currentSavings[direction] || !currentSavings[direction].scenarios) return null;
    const sc = currentSavings[direction].scenarios;
    if (!sc.length) return null;
    const exact = sc.find(s => Math.abs(s.delta_f - deltaF) < 0.01);
    if (exact) return exact;
    const sorted = [...sc].sort((a,b) => a.delta_f - b.delta_f);
    if (deltaF >= sorted[0].delta_f && deltaF <= sorted[sorted.length - 1].delta_f) {
      let lo = sorted[0], hi = sorted[sorted.length - 1];
      for (let i = 0; i < sorted.length - 1; i++) {
        if (sorted[i].delta_f <= deltaF && sorted[i+1].delta_f >= deltaF) {
          lo = sorted[i]; hi = sorted[i+1]; break;
        }
      }
      if (lo.delta_f === hi.delta_f) return lo;
      const t = (deltaF - lo.delta_f) / (hi.delta_f - lo.delta_f);
      return {
        delta_f: deltaF,
        monthly_kwh_delta:  lo.monthly_kwh_delta  + t * (hi.monthly_kwh_delta  - lo.monthly_kwh_delta),
        monthly_cost_delta: lo.monthly_cost_delta + t * (hi.monthly_cost_delta - lo.monthly_cost_delta),
      };
    }
    // Extrapolate
    const useLow = deltaF < sorted[0].delta_f;
    const a = useLow ? sorted[0] : sorted[sorted.length - 2];
    const b = useLow ? sorted[1] : sorted[sorted.length - 1];
    if (a.delta_f === b.delta_f) return a;
    const slopeKwh  = (b.monthly_kwh_delta  - a.monthly_kwh_delta)  / (b.delta_f - a.delta_f);
    const slopeCost = (b.monthly_cost_delta - a.monthly_cost_delta) / (b.delta_f - a.delta_f);
    return {
      delta_f: deltaF,
      monthly_kwh_delta:  a.monthly_kwh_delta  + slopeKwh  * (deltaF - a.delta_f),
      monthly_cost_delta: a.monthly_cost_delta + slopeCost * (deltaF - a.delta_f),
    };
  }

  function _entitySetpoints(e) {
    if (!e) return { cool: null, heat: null };
    if (e.target_low_f !== null && e.target_low_f !== undefined &&
        e.target_high_f !== null && e.target_high_f !== undefined) {
      return { cool: e.target_high_f, heat: e.target_low_f };
    }
    const m = (e.hvac_mode || '').toLowerCase();
    if (m === 'heat') return { cool: null, heat: e.target_temp_f };
    if (m === 'cool') return { cool: e.target_temp_f, heat: null };
    return { cool: e.target_temp_f, heat: e.target_temp_f };
  }

  function renderSetpointControl(entities, savings, bounds, unit) {
    const card = $('setpoint-control-card');
    if (!entities.length && !(savings && (savings.cooling || savings.heating))) {
      card.style.display = 'none'; return;
    }
    card.style.display = '';
    currentEntities = entities;
    currentSavings = savings;
    // Fall back to default bounds if endpoint hasn't loaded
    savingsBounds = bounds || { cool_min_f: 60, cool_max_f: 85, heat_min_f: 55, heat_max_f: 80, ha_api_available: false };
    savingsUnit = unit;

    $('setpoint-needs-api').style.display = savingsBounds.ha_api_available ? 'none' : '';

    const sel = $('setpoint-entity-select');
    if (entities.length) {
      const prev = sel.value;
      sel.innerHTML = entities.map(e => {
        const action = e.hvac_action ? ` · ${e.hvac_action}` : '';
        const mode = e.hvac_mode ? ` (${e.hvac_mode})` : '';
        return `<option value="${e.entity_id}">${e.entity_id}${mode}${action}</option>`;
      }).join('');
      sel.value = prev && entities.find(e => e.entity_id === prev) ? prev : entities[0].entity_id;
    } else {
      sel.innerHTML = `<option value="">(no thermostat data yet)</option>`;
    }

    _refreshControlsFromSelection();
    _renderDialTicks();
    _redrawDial();
  }

  function _refreshControlsFromSelection() {
    const sel = $('setpoint-entity-select');
    const ent = currentEntities.find(e => e.entity_id === sel.value);
    const sp = _entitySetpoints(ent);

    // Fall back to savings model's default-setpoint when no live data
    if (sp.cool === null && currentSavings && currentSavings.cooling && currentSavings.cooling.current_setpoint_f != null) {
      sp.cool = currentSavings.cooling.current_setpoint_f;
    }
    if (sp.heat === null && currentSavings && currentSavings.heating && currentSavings.heating.current_setpoint_f != null) {
      sp.heat = currentSavings.heating.current_setpoint_f;
    }
    originalCool = sp.cool;
    originalHeat = sp.heat;
    desiredCoolF = sp.cool;
    desiredHeatF = sp.heat;

    // Mode toggle availability
    const coolBtn = $('mode-cool');
    const heatBtn = $('mode-heat');
    coolBtn.disabled = (sp.cool === null);
    heatBtn.disabled = (sp.heat === null);
    $('mode-cool-current').textContent = sp.cool !== null ? fmtTemp(sp.cool, savingsUnit) : '—';
    $('mode-heat-current').textContent = sp.heat !== null ? fmtTemp(sp.heat, savingsUnit) : '—';

    // Pick default active mode based on what's available
    if (sp.cool !== null && !coolBtn.disabled) dialMode = 'cool';
    else if (sp.heat !== null) dialMode = 'heat';

    _setActiveModeUI();
    _refreshScenarios();
  }

  function _setActiveModeUI() {
    document.querySelectorAll('.mode-pill').forEach(b => b.classList.remove('active'));
    const btn = $('mode-' + dialMode);
    if (btn) btn.classList.add('active');
    const dial = $('thermostat-dial');
    dial.classList.remove('cool', 'heat');
    dial.classList.add(dialMode);
  }

  function _activeSetpoint() {
    return dialMode === 'cool' ? desiredCoolF : desiredHeatF;
  }
  function _setActiveSetpoint(value) {
    const bounds = _activeBounds();
    if (!bounds) return;
    const v = Math.max(bounds.min, Math.min(bounds.max, value));
    if (dialMode === 'cool') desiredCoolF = v;
    else desiredHeatF = v;
  }
  function _originalSetpoint() {
    return dialMode === 'cool' ? originalCool : originalHeat;
  }

  function _renderDialTicks() {
    const g = $('thermostat-ticks');
    g.innerHTML = '';
    const bounds = _activeBounds();
    if (!bounds) return;
    const range = bounds.max - bounds.min;
    // One tick per degree; bolder every 5
    for (let t = bounds.min; t <= bounds.max; t++) {
      const angle = DIAL_START_DEG + ((t - bounds.min) / range) * (DIAL_END_DEG - DIAL_START_DEG);
      const inner = _polar(DIAL_CX, DIAL_CY, DIAL_R, angle);
      const outer = _polar(DIAL_CX, DIAL_CY, DIAL_R + ((t % 5 === 0) ? 14 : 8), angle);
      const cls = (t % 5 === 0) ? 'active' : '';
      g.insertAdjacentHTML('beforeend',
        `<line x1="${inner.x.toFixed(1)}" y1="${inner.y.toFixed(1)}" ` +
        `x2="${outer.x.toFixed(1)}" y2="${outer.y.toFixed(1)}" class="${cls}"/>`);
    }
  }

  function _redrawDial() {
    const sp = _activeSetpoint();
    const orig = _originalSetpoint();
    const bounds = _activeBounds();
    if (sp == null || !bounds) {
      $('dial-setpoint-text').textContent = '—';
      $('dial-mode-text').textContent = '—';
      $('thermostat-arc').setAttribute('d', '');
      return;
    }
    $('dial-setpoint-text').textContent = `${Math.round(sp)}°`;
    $('dial-mode-text').textContent = (dialMode === 'cool' ? 'COOL TO' : 'HEAT TO');
    // Show current entity's current_temp_f as inside temp
    const sel = $('setpoint-entity-select');
    const ent = currentEntities.find(e => e.entity_id === sel.value);
    if (ent && ent.current_temp_f != null) {
      $('dial-current-text').textContent = `inside ${fmtTemp(ent.current_temp_f, savingsUnit)}`;
    } else {
      $('dial-current-text').textContent = '';
    }

    // Arc from "original" angle to "desired" angle
    const origAngle = _tempToAngle(orig);
    const newAngle = _tempToAngle(sp);
    const a0 = _polar(DIAL_CX, DIAL_CY, DIAL_R, origAngle);
    const a1 = _polar(DIAL_CX, DIAL_CY, DIAL_R, newAngle);
    const sweep = (newAngle > origAngle) ? 1 : 0;
    const largeArc = Math.abs(newAngle - origAngle) > 180 ? 1 : 0;
    const arc = `M ${a0.x.toFixed(1)} ${a0.y.toFixed(1)} A ${DIAL_R} ${DIAL_R} 0 ${largeArc} ${sweep} ${a1.x.toFixed(1)} ${a1.y.toFixed(1)}`;
    $('thermostat-arc').setAttribute('d', arc);

    // Handle position at current desired
    const handle = $('thermostat-handle');
    handle.setAttribute('cx', a1.x.toFixed(1));
    handle.setAttribute('cy', a1.y.toFixed(1));
  }

  function _refreshScenarios() {
    const sym = (currentSavings && currentSavings.currency_symbol) || '$';
    const sp = _activeSetpoint();
    const orig = _originalSetpoint();
    if (sp == null || orig == null) {
      $('scenario-headline').textContent = '—';
      $('scenario-detail').textContent = 'Pick a thermostat to start.';
      $('scenario-method').textContent = '';
      $('scenario-card').classList.remove('save', 'cost');
      $('scenario-headline').classList.remove('save', 'cost');
      _redrawDial();
      return;
    }
    const dF = sp - orig;
    const card = $('scenario-card');
    const headline = $('scenario-headline');
    card.classList.remove('save', 'cost');
    headline.classList.remove('save', 'cost');
    if (Math.abs(dF) < 0.01) {
      headline.textContent = 'No change';
      $('scenario-detail').textContent = 'Move the dial to preview the impact.';
    } else {
      const direction = dialMode === 'cool' ? 'cooling' : 'heating';
      const scenario = _approxScenario(direction, dF);
      if (!scenario) {
        headline.textContent = `${dF > 0 ? '+' : ''}${dF.toFixed(0)}°`;
        $('scenario-detail').textContent = 'Not enough data yet to estimate cost impact.';
      } else {
        const c = scenario.monthly_cost_delta;
        const k = scenario.monthly_kwh_delta;
        headline.textContent = `${_fmtMoney(c, sym)}/mo`;
        $('scenario-detail').textContent =
          `${dF > 0 ? '+' : ''}${dF.toFixed(0)}°F · ${k >= 0 ? '+' : ''}${k.toFixed(1)} kWh/month`;
        const cls = c <= 0 ? 'save' : 'cost';
        card.classList.add(cls);
        headline.classList.add(cls);
      }
    }
    // Method note
    if (currentSavings) {
      const dirKey = dialMode === 'cool' ? 'cooling' : 'heating';
      const s = currentSavings[dirKey];
      if (s && s.has_model) {
        $('scenario-method').textContent =
          `Fitted regression · R² ${s.r_squared.toFixed(2)}, n=${s.n}`;
      } else if (s && s.method === 'rule_of_thumb_5pct') {
        const basis = (s.scenarios && s.scenarios[0] && s.scenarios[0].basis) || 'rule_of_thumb';
        $('scenario-method').textContent = basis === 'role_energy'
          ? 'Rule of thumb (~5%/°F) on your tagged HVAC device — fitted regression activates once you have ~14 days with setpoint variance.'
          : basis === 'no_data'
          ? 'Need a few days of energy data to estimate — please wait.'
          : 'Rule of thumb (~5%/°F) on whole-panel energy × HVAC fraction — tag a cooling/heating device for tighter estimates.';
      } else if (s) {
        $('scenario-method').textContent = s.needs || '';
      }
    }
    _redrawDial();
  }

  // --- Dial drag interaction ---
  let dragging = false;
  function _dialPointerToAngle(evt) {
    const svg = $('thermostat-dial');
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const local = pt.matrixTransform(ctm.inverse());
    const dx = local.x - DIAL_CX;
    const dy = local.y - DIAL_CY;
    let ang = Math.atan2(dy, dx) * 180 / Math.PI;
    // Clamp to dial sweep
    // Normalise angle to be near our start..end range (start is -210 ≈ 150)
    if (ang < -180) ang += 360;
    if (ang > 180) ang -= 360;
    // Our sweep: -210 → 30, which after normalization means clamp to:
    //   ang in [-180, 30] OR [150, 180]
    // Easier: shift everything by 90 and clamp continuously.
    const shift = (a) => {
      let x = a + 210;            // start at 0
      if (x < 0) x += 360;
      return x;                   // 0..360, sweep is 0..240
    };
    const xx = shift(ang);
    const clamped = Math.max(0, Math.min(240, xx));
    return DIAL_START_DEG + clamped;
  }

  function _initSetpointControlsOnce() {
    if (_initSetpointControlsOnce._done) return;
    _initSetpointControlsOnce._done = true;
    const sel = $('setpoint-entity-select');
    if (!sel) return;
    sel.addEventListener('change', _refreshControlsFromSelection);

    $('mode-cool').addEventListener('click', () => {
      if ($('mode-cool').disabled) return;
      dialMode = 'cool';
      _setActiveModeUI();
      _renderDialTicks();
      _refreshScenarios();
    });
    $('mode-heat').addEventListener('click', () => {
      if ($('mode-heat').disabled) return;
      dialMode = 'heat';
      _setActiveModeUI();
      _renderDialTicks();
      _refreshScenarios();
    });

    $('dial-minus').addEventListener('click', () => {
      const cur = _activeSetpoint();
      if (cur == null) return;
      _setActiveSetpoint(cur - 1);
      _refreshScenarios();
    });
    $('dial-plus').addEventListener('click', () => {
      const cur = _activeSetpoint();
      if (cur == null) return;
      _setActiveSetpoint(cur + 1);
      _refreshScenarios();
    });

    // Drag the handle
    const svg = $('thermostat-dial');
    function onMove(e) {
      if (!dragging) return;
      e.preventDefault();
      const ang = _dialPointerToAngle(e);
      if (ang == null) return;
      const t = _angleToTemp(ang);
      if (t == null) return;
      _setActiveSetpoint(Math.round(t));
      _refreshScenarios();
    }
    svg.addEventListener('pointerdown', (e) => {
      // Only respond when clicking on the handle or near the ring
      dragging = true;
      svg.setPointerCapture(e.pointerId);
      onMove(e);
    });
    svg.addEventListener('pointermove', onMove);
    svg.addEventListener('pointerup', (e) => {
      dragging = false;
      try { svg.releasePointerCapture(e.pointerId); } catch (_) {}
    });
    svg.addEventListener('pointercancel', () => { dragging = false; });

    $('setpoint-reset').addEventListener('click', () => {
      _refreshControlsFromSelection();
      $('setpoint-status').textContent = '';
    });

    $('setpoint-apply').addEventListener('click', async () => {
      const entity = $('setpoint-entity-select').value;
      if (!entity) {
        $('setpoint-status').textContent = 'No thermostat selected.';
        return;
      }
      const ent = currentEntities.find(e => e.entity_id === entity);
      const payload = { entity_id: entity, ha_temp_unit: savingsUnit };
      // Send only what changed in the active mode; keep the other side at
      // its original value so heat_cool entities don't get a partial write.
      const coolChanged = desiredCoolF !== null && originalCool !== null && Math.abs(desiredCoolF - originalCool) >= 0.5;
      const heatChanged = desiredHeatF !== null && originalHeat !== null && Math.abs(desiredHeatF - originalHeat) >= 0.5;
      if (!coolChanged && !heatChanged) {
        $('setpoint-status').textContent = 'No change to apply.';
        return;
      }
      if (ent && ent.target_high_f !== null && ent.target_high_f !== undefined) {
        // dual setpoint mode
        payload.target_high_f = desiredCoolF !== null ? desiredCoolF : ent.target_high_f;
        payload.target_low_f  = desiredHeatF !== null ? desiredHeatF : ent.target_low_f;
      } else {
        // single setpoint — use whichever direction changed
        payload.target_temp_f = (dialMode === 'cool') ? desiredCoolF : desiredHeatF;
      }
      $('setpoint-apply').disabled = true;
      $('setpoint-status').textContent = 'Sending…';
      try {
        const r = await fetch(API + '/setpoint/set', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          $('setpoint-status').textContent = `Failed: ${j.detail || r.statusText}`;
        } else if (j.needs_api) {
          $('setpoint-status').textContent = j.message || 'HA API permission needed.';
        } else {
          $('setpoint-status').textContent = 'Applied. Thermostat will catch up within a minute.';
          setTimeout(loadClimate, 5000);
        }
      } catch (e) {
        $('setpoint-status').textContent = `Error: ${e}`;
      } finally {
        $('setpoint-apply').disabled = false;
      }
    });
  }
  document.addEventListener('DOMContentLoaded', _initSetpointControlsOnce);
  _initSetpointControlsOnce();

  function renderClimateNow(now, unit) {
    if (!now || now.temp_f === null || now.temp_f === undefined) {
      $('climate-now-temp').textContent = '—';
      $('climate-now-meta').textContent = 'No weather data yet';
      $('climate-hilo').textContent = '—';
      $('climate-avg').textContent = '—';
      $('climate-hdd').textContent = '—';
      $('climate-hdd-meta').textContent = '—';
      $('climate-cdd').textContent = '—';
      $('climate-cdd-meta').textContent = '—';
      return;
    }
    $('climate-now-temp').textContent = fmtTemp(now.temp_f, unit);
    const parts = [];
    if (now.condition) parts.push(now.condition);
    if (now.humidity !== null && now.humidity !== undefined) parts.push(`${Math.round(now.humidity)}% RH`);
    $('climate-now-meta').textContent = parts.join(' · ') || '—';

    // H/L: prefer forecast (meaningful early in the day), fall back to
    // empirical min/max. If they're equal-ish, it's because we only have one
    // sample — show a hint so the UI isn't confusing.
    let highF = (now.today_forecast_high_f !== null && now.today_forecast_high_f !== undefined)
      ? now.today_forecast_high_f : now.today_max_f;
    let lowF = (now.today_forecast_low_f !== null && now.today_forecast_low_f !== undefined)
      ? now.today_forecast_low_f : now.today_min_f;
    const sameVal = (highF !== null && lowF !== null && Math.abs(highF - lowF) < 0.5 &&
                     now.temp_f !== null && Math.abs(now.temp_f - highF) < 0.5);
    if (sameVal) {
      // Not enough variation to be meaningful yet
      $('climate-hilo').textContent = `${fmtTemp(now.temp_f, unit)}`;
      $('climate-avg').textContent = 'still collecting today\'s range';
    } else {
      $('climate-hilo').textContent =
        `${fmtTemp(highF, unit)} / ${fmtTemp(lowF, unit)}`;
      const source = (now.today_forecast_high_f !== null && now.today_forecast_high_f !== undefined)
        ? 'forecast' : 'observed';
      $('climate-avg').textContent = now.today_avg_f !== null
        ? `avg ${fmtTemp(now.today_avg_f, unit)} · ${source}`
        : source;
    }

    $('climate-hdd').textContent = fmtDD(now.today_hdd);
    $('climate-cdd').textContent = fmtDD(now.today_cdd);
    const baseLabel = unit === 'C'
      ? `${((now.base_temp_f - 32) * 5/9).toFixed(1)}°C`
      : `${now.base_temp_f.toFixed(0)}°F`;
    $('climate-hdd-meta').textContent = `vs ${baseLabel} base`;
    $('climate-cdd-meta').textContent = `vs ${baseLabel} base`;
  }

  function renderClimateAnomaly(anom) {
    const banner = $('climate-anomaly');
    const panel = anom && anom.panel;
    if (!panel || !panel.model || panel.verdict === 'insufficient_baseline' || panel.verdict === 'unavailable') {
      banner.style.display = 'none';
      return;
    }
    banner.style.display = '';
    banner.classList.remove('above', 'below', 'normal');
    if (panel.verdict === 'above_baseline') banner.classList.add('above');
    else if (panel.verdict === 'below_baseline') banner.classList.add('below');
    else banner.classList.add('normal');

    $('anomaly-predicted').textContent = panel.predicted_kwh_so_far !== undefined
      ? `${panel.predicted_kwh_so_far.toFixed(2)} kWh` : '—';
    $('anomaly-actual').textContent = `${(panel.today_actual_kwh || 0).toFixed(2)} kWh`;
    if (panel.delta_pct !== null && panel.delta_pct !== undefined) {
      const sign = panel.delta_pct >= 0 ? '+' : '';
      $('anomaly-delta').textContent = `${sign}${panel.delta_pct.toFixed(0)}% (${panel.delta_kwh.toFixed(2)} kWh)`;
    } else {
      $('anomaly-delta').textContent = '—';
    }
    $('anomaly-r2').textContent = panel.model.r_squared !== undefined
      ? panel.model.r_squared.toFixed(2) : '—';

    const explain = [];
    if (panel.verdict === 'above_baseline') {
      explain.push('Today is running well above what the temperature would predict.');
      explain.push('Could be a new device running, a stuck appliance, or a guest cycle.');
    } else if (panel.verdict === 'below_baseline') {
      explain.push('Today is running well below the temperature-predicted baseline.');
    } else {
      explain.push('Today\'s usage matches what the temperature predicts.');
    }
    explain.push(`Model fit on last ${anom.history_days} completed days.`);
    $('anomaly-explain').textContent = explain.join(' ');
  }

  function _setpointColorFor(setpointF, unit, lo, hi) {
    // Map setpoint into the lo..hi range, return a viridis-ish hex code.
    if (setpointF === null || setpointF === undefined) return '#7c8896';
    const t = Math.max(0, Math.min(1, (setpointF - lo) / Math.max(1, (hi - lo))));
    // Cool: blue (low setpoint = more cooling) -> orange (high setpoint = less cooling)
    const r = Math.round(40  + t * (240 - 40));
    const g = Math.round(120 + t * (140 - 120));
    const b = Math.round(220 - t * (220 - 60));
    return `rgb(${r},${g},${b})`;
  }

  function _verdictRow(label, anomSection, kwhSuffix) {
    if (!anomSection || anomSection.delta_pct === undefined || anomSection.delta_pct === null) {
      return ['—', '—'];
    }
    const sign = anomSection.delta_pct >= 0 ? '+' : '';
    return [
      `${sign}${anomSection.delta_pct.toFixed(0)}% (${(anomSection.delta_kwh||0).toFixed(2)} ${kwhSuffix || 'kWh'})`,
      anomSection.model && anomSection.model.r_squared !== undefined
        ? `R² ${anomSection.model.r_squared.toFixed(2)}, n=${anomSection.model.n}` : '—',
    ];
  }

  function renderCoolingSection(now, anom, days, devs, unit) {
    const cooling = (devs || []).find(d => d.hvac_role === 'cooling') ||
                    (devs || []).find(d => d.is_hvac);   // legacy fallback
    const card = $('climate-cooling-card');
    if (!cooling) { card.style.display = 'none'; return; }
    card.style.display = '';
    $('climate-cooling-name').textContent = cooling.name;
    const todayKwh = now ? (now.today_cooling_kwh || 0) : 0;
    $('cooling-today').textContent = `${todayKwh.toFixed(2)} kWh`;
    const sp = now && now.today_avg_cool_setpoint_f;
    $('cooling-setpoint').textContent = sp != null
      ? `avg setpoint ${fmtTemp(sp, unit)}`
      : 'no setpoint data yet';
    const [deltaTxt, fitTxt] = _verdictRow('Cooling', anom && anom.cooling, 'kWh');
    $('cooling-delta').textContent = deltaTxt;
    $('cooling-r2').textContent = fitTxt;

    // Scatter: cooling_wh / day vs avg temp, points colored by avg cool setpoint
    const points = days
      .filter(d => d.avg_temp_f !== null && d.cooling_wh && d.cooling_wh > 0)
      .map(d => ({
        x: unit === 'C' ? ((d.avg_temp_f - 32) * 5/9) : d.avg_temp_f,
        y: d.cooling_wh / 1000.0,
        date: d.date_str,
        setF: d.avg_cool_setpoint_f,
        bg: _setpointColorFor(d.avg_cool_setpoint_f, unit, 65, 80),
      }));
    if (climateCoolingScatterChart) climateCoolingScatterChart.destroy();
    climateCoolingScatterChart = new Chart($('climate-cooling-scatter'), {
      type: 'scatter',
      data: { datasets: [{
        label: 'Cooling kWh / day',
        data: points,
        backgroundColor: points.map(p => p.bg),
        pointRadius: 5,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: ctx => {
              const p = ctx.raw;
              const sp = p.setF != null ? ` · setpoint ${fmtTemp(p.setF, unit)}` : '';
              return `${p.date}: ${p.y.toFixed(2)} kWh @ ${p.x.toFixed(1)}°${unit}${sp}`;
            },
          }},
        },
        scales: {
          x: { title: { display: true, text: `Daily avg outside temp (°${unit})`, color: '#8a93a6' }, ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' } },
          y: { title: { display: true, text: 'Cooling kWh', color: '#8a93a6' }, ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, beginAtZero: true },
        },
      },
    });
  }

  function renderHeatingSection(now, anom, days, devs, unit) {
    const heating = (devs || []).find(d => d.hvac_role === 'heating');
    const hasGas = days.some(d => d.heating_therms && d.heating_therms > 0) ||
                   (now && now.today_heating_therms && now.today_heating_therms > 0);
    const card = $('climate-heating-card');
    if (!heating && !hasGas) { card.style.display = 'none'; return; }
    card.style.display = '';

    const useGas = hasGas && !heating;     // gas-only display when no electric heating device
    $('climate-heating-name').textContent = heating ? heating.name : 'natural-gas heating';
    $('heating-units-note').textContent = useGas
      ? 'Heating shown in therms — convert to your unit via the add-on options.'
      : 'Heating shown in kWh.';

    if (useGas) {
      const therms = now ? (now.today_heating_therms || 0) : 0;
      $('heating-today').textContent = `${therms.toFixed(2)} therms`;
    } else {
      const todayKwh = now ? (now.today_heating_kwh || 0) : 0;
      $('heating-today').textContent = `${todayKwh.toFixed(2)} kWh`;
    }
    const sp = now && now.today_avg_heat_setpoint_f;
    $('heating-setpoint').textContent = sp != null
      ? `avg setpoint ${fmtTemp(sp, unit)}`
      : 'no setpoint data yet';
    const [deltaTxt, fitTxt] = _verdictRow('Heating', anom && anom.heating, useGas ? 'therms' : 'kWh');
    $('heating-delta').textContent = deltaTxt;
    $('heating-r2').textContent = fitTxt;

    // Scatter: heating_wh or therms / day vs avg temp
    const points = days
      .filter(d => d.avg_temp_f !== null && ((useGas && d.heating_therms > 0) || (!useGas && d.heating_wh && d.heating_wh > 0)))
      .map(d => ({
        x: unit === 'C' ? ((d.avg_temp_f - 32) * 5/9) : d.avg_temp_f,
        y: useGas ? d.heating_therms : (d.heating_wh / 1000.0),
        date: d.date_str,
        setF: d.avg_heat_setpoint_f,
        bg: _setpointColorFor(d.avg_heat_setpoint_f, unit, 60, 75),
      }));
    if (climateHeatingScatterChart) climateHeatingScatterChart.destroy();
    climateHeatingScatterChart = new Chart($('climate-heating-scatter'), {
      type: 'scatter',
      data: { datasets: [{
        label: useGas ? 'Heating therms / day' : 'Heating kWh / day',
        data: points,
        backgroundColor: points.map(p => p.bg),
        pointRadius: 5,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: ctx => {
              const p = ctx.raw;
              const sp = p.setF != null ? ` · setpoint ${fmtTemp(p.setF, unit)}` : '';
              return `${p.date}: ${p.y.toFixed(2)} ${useGas ? 'therms' : 'kWh'} @ ${p.x.toFixed(1)}°${unit}${sp}`;
            },
          }},
        },
        scales: {
          x: { title: { display: true, text: `Daily avg outside temp (°${unit})`, color: '#8a93a6' }, ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' } },
          y: { title: { display: true, text: useGas ? 'therms' : 'kWh', color: '#8a93a6' }, ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, beginAtZero: true },
        },
      },
    });
  }

  let climateCoolingScatterChart = null;
  let climateHeatingScatterChart = null;
  let climateSetpointChart = null;

  function renderSetpointTimeline(days, unit) {
    const card = $('climate-setpoint-card');
    const hasAny = days.some(d => d.avg_cool_setpoint_f !== null || d.avg_heat_setpoint_f !== null);
    if (!hasAny) { card.style.display = 'none'; return; }
    card.style.display = '';
    const labels = days.map(d => d.date_str.slice(5));
    const cool = days.map(d => d.avg_cool_setpoint_f !== null && d.avg_cool_setpoint_f !== undefined
      ? (unit === 'C' ? (d.avg_cool_setpoint_f - 32) * 5/9 : d.avg_cool_setpoint_f) : null);
    const heat = days.map(d => d.avg_heat_setpoint_f !== null && d.avg_heat_setpoint_f !== undefined
      ? (unit === 'C' ? (d.avg_heat_setpoint_f - 32) * 5/9 : d.avg_heat_setpoint_f) : null);
    const outside = days.map(d => d.avg_temp_f !== null && d.avg_temp_f !== undefined
      ? (unit === 'C' ? (d.avg_temp_f - 32) * 5/9 : d.avg_temp_f) : null);
    if (climateSetpointChart) climateSetpointChart.destroy();
    climateSetpointChart = new Chart($('climate-setpoint-chart'), {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'Outside avg', data: outside, borderColor: '#8a93a6', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderDash: [4,4] },
          { label: 'Cool setpoint', data: cool, borderColor: '#4ea1d3', backgroundColor: '#4ea1d320', tension: 0.3, pointRadius: 2 },
          { label: 'Heat setpoint', data: heat, borderColor: '#e15759', backgroundColor: '#e1575920', tension: 0.3, pointRadius: 2 },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#e6e9ef' }}},
        scales: {
          x: { ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' } },
          y: { title: { display: true, text: `Temperature (°${unit})`, color: '#8a93a6' }, ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' } },
        },
      },
    });
  }

  function renderClimateScatter(days, unit) {
    const canvas = $('climate-scatter');
    if (!canvas) return;
    const points = days
      .filter(d => d.avg_temp_f !== null && d.panel_wh > 0)
      .map(d => ({
        x: unit === 'C' ? ((d.avg_temp_f - 32) * 5/9) : d.avg_temp_f,
        y: (d.panel_wh / 1000.0),
        date: d.date_str,
      }));
    if (climateScatterChart) climateScatterChart.destroy();
    climateScatterChart = new Chart(canvas, {
      type: 'scatter',
      data: { datasets: [{
        label: 'Panel kWh / day',
        data: points,
        backgroundColor: '#4e79a7',
        pointRadius: 5,
      }]},
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: {
            label: ctx => `${ctx.raw.date}: ${ctx.raw.y.toFixed(2)} kWh @ ${ctx.raw.x.toFixed(1)}°${unit}`,
          }},
        },
        scales: {
          x: { title: { display: true, text: `Daily avg outside temp (°${unit})`, color: '#8a93a6' }, ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' } },
          y: { title: { display: true, text: 'kWh', color: '#8a93a6' }, ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, beginAtZero: true },
        },
      },
    });
  }

  function renderClimateDegreeBars(days, unit) {
    const canvas = $('climate-degree-bars');
    if (!canvas) return;
    const recent = days.slice(-14);   // last 14 days
    const labels = recent.map(d => d.date_str.slice(5));   // MM-DD
    const hdd = recent.map(d => d.hdd || 0);
    const cdd = recent.map(d => d.cdd || 0);
    if (climateDegreeBarsChart) climateDegreeBarsChart.destroy();
    climateDegreeBarsChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          { label: 'HDD', data: hdd, backgroundColor: '#4e79a7' },
          { label: 'CDD', data: cdd, backgroundColor: '#e15759' },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#e6e9ef' }}},
        scales: {
          x: { ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, stacked: true },
          y: { ticks: { color: '#8a93a6' }, grid: { color: '#2a3340' }, stacked: true, beginAtZero: true },
        },
      },
    });
  }

  // (legacy renderClimateHvac removed — superseded by renderCoolingSection /
  // renderHeatingSection which split by hvac_role and include setpoint coloring)

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
    $('edit-hvac-role').value = d.hvac_role || (d.is_hvac ? 'cooling' : '');
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
      hvac_role: $('edit-hvac-role').value,
    };
    const r = await fetch(API + '/devices/' + editingDeviceId, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      $('modal-edit').classList.add('hidden');
      loadDevices();
      // The HVAC tag changes which device the daily rollups attribute hvac_wh
      // to — rebuild the recent rollups so the Climate tab shows fresh data
      // immediately rather than waiting for the next nightly tick.
      fetch(API + '/weather/rebuild_rollups', { method: 'POST' }).catch(() => {});
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
