(() => {
  const cfg = window.APP_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const API = 'api';

  // --- Tabs ---
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      $('tab-' + btn.dataset.tab).classList.add('active');
      if (btn.dataset.tab === 'devices') loadDevices();
      if (btn.dataset.tab === 'clusters') loadClusters();
      if (btn.dataset.tab === 'history') loadHistory();
    });
  });

  // --- Live updates ---
  const liveBuf = [];
  let liveChart;

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
        plugins: { legend: { labels: { color: '#e6e9ef' } } }
      }
    });
  }

  async function pollLive() {
    try {
      const r = await fetch(API + '/live');
      if (!r.ok) throw new Error('bad status');
      const s = await r.json();
      $('conn-indicator').textContent = 'live';
      $('conn-indicator').className = 'ok';

      $('total-power').textContent = fmt(s.total_power);
      $('total-current').textContent = fmt(s.total_current, 2);
      $('a-power').textContent = fmt(s.a_power);
      $('b-power').textContent = fmt(s.b_power);
      $('c-power').textContent = fmt(s.c_power);
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
        const t = new Date(s.ts * 1000).toLocaleTimeString();
        liveBuf.push({ t, a: s.a_power, b: s.b_power, c: s.c_power, total: s.total_power });
        if (liveBuf.length > 180) liveBuf.shift();
        liveChart.data.labels = liveBuf.map(p => p.t);
        liveChart.data.datasets[0].data = liveBuf.map(p => p.a);
        liveChart.data.datasets[1].data = liveBuf.map(p => p.b);
        liveChart.data.datasets[2].data = liveBuf.map(p => p.c);
        liveChart.data.datasets[3].data = liveBuf.map(p => p.total);
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

  // --- Devices ---
  async function loadDevices() {
    const r = await fetch(API + '/devices');
    const devices = await r.json();
    const root = $('devices-list');
    if (!devices.length) {
      root.innerHTML = `<div class="empty">No devices yet. Label a cluster to start tracking one.</div>`;
      return;
    }
    root.innerHTML = devices.map(d => {
      const tags = [];
      if (d.is_continuous) tags.push('<span class="muted" style="font-size:0.7rem;padding:2px 6px;border:1px solid var(--border);border-radius:4px;">CONTINUOUS</span>');
      if (d.source_entity_id) tags.push(`<span class="muted" style="font-size:0.7rem;">via ${escapeHtml(d.source_entity_id)}</span>`);
      return `
      <div class="list-item">
        <div class="device-state ${d.is_on ? 'on' : ''}">${d.is_on ? 'ON' : 'OFF'}</div>
        <div>
          <div class="name">${escapeHtml(d.name)} ${tags.join(' ')}</div>
          <div class="meta">
            ${Math.round(d.mean_power_w || 0)} W ·
            ${d.notes ? escapeHtml(d.notes) + ' · ' : ''}
            ${d.last_on_ts ? 'last on ' + new Date(d.last_on_ts * 1000).toLocaleString() : 'never seen on'}
          </div>
        </div>
        <div>
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
    const r = await fetch(API + '/history?minutes=' + minutes);
    const data = await r.json();
    const labels = data.map(d => new Date(d.ts * 1000).toLocaleTimeString());
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
        plugins: { legend: { labels: { color: '#e6e9ef' } } }
      }
    };
    if (historyChart) historyChart.destroy();
    historyChart = new Chart($('history-chart'), cfg2);
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

  // --- boot ---
  initLiveChart();
  pollLive();
  pollStats();
  setInterval(pollLive, 1500);
  setInterval(pollStats, 15000);
  setInterval(() => {
    if (document.querySelector('.tab.active').dataset.tab === 'devices') loadDevices();
  }, 5000);
})();
