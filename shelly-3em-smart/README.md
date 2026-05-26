# Shelly Pro 3EM Smart Monitor

A Docker container that streams data from a Shelly Pro 3EM, auto-detects connected
appliances from their power signatures, exposes a built-in dashboard, and publishes
inferred devices to Home Assistant over MQTT.

## How it works

```
Shelly Pro 3EM ──ws──▶  collector ──▶ SQLite
                           │
                           ├──▶ step-event detector ──▶ DBSCAN clusterer ──▶ "candidate devices"
                           │                                                       │
                           │                                                       ▼
                           │                                              you label them once
                           │                                                       │
                           ├──▶ live matcher ─────────────────────────────────────┘
                           │                              │
                           ├──▶ FastAPI dashboard ◀──────┤
                           └──▶ MQTT (HA discovery) ◀────┘
```

1. **Collector** holds a websocket open to the Shelly's RPC port and receives a
   sample roughly every second (voltage, current, power, power factor per channel).
2. **Event detector** watches total active power and emits an `on` or `off` event
   whenever it sees a clean step change larger than `STEP_THRESHOLD_W`.
3. **Clusterer** runs hourly. It groups similar events into clusters using DBSCAN
   over (Δpower, per-channel Δpower, power factor). Each cluster is a probable
   appliance.
4. **You label them.** Open the dashboard → "Unlabeled Clusters" tab → click
   *Label*. From that point on the matcher tags every new event with the right
   device and publishes its on/off state.
5. **Home Assistant** auto-discovers each labelled device as a `binary_sensor` and
   each channel as live `power` / `current` sensors.

## Quick start

1. Find your Shelly Pro 3EM's local IP (e.g. via the Shelly app or your router).
2. Edit `docker-compose.yml` and set `SHELLY_HOST`. If you want the HA integration,
   uncomment the `MQTT_*` block and fill in your broker.
3. Bring it up:
   ```sh
   docker compose up -d --build
   ```
4. Open <http://localhost:8080>.

The SQLite database lives in `./data/shelly.db` — back that up, that's where your
labelled devices and history live.

## Channel mapping

The Pro 3EM has three channels (A, B, C). On a US 240 V split-phase panel a clamp
goes on each hot leg:

| Shelly channel | Wire        | Default label |
| -------------- | ----------- | ------------- |
| A              | L1 hot      | `L1`          |
| B              | L2 hot      | `L2`          |
| C              | optional    | `Spare`       |

The third channel is free for a sub-panel feeder, a dedicated 120 V circuit, or
left disconnected. Set the labels to whatever makes sense for your install via
`CHANNEL_A_LABEL` / `CHANNEL_B_LABEL` / `CHANNEL_C_LABEL`.

Note: a 240 V appliance (well pump, dryer, electric range) draws on **both** A
and B simultaneously, so its cluster signature shows roughly equal Δpower on A
and B. A 120 V appliance shows the entire step on a single leg. The clusterer
sees this and treats them as different signatures automatically — useful for
distinguishing devices that have similar total wattage.

## Detection tuning

These knobs live in `docker-compose.yml` and can all be edited without
rebuilding the image:

| Env var               | Default | What it does                                                  |
| --------------------- | ------- | ------------------------------------------------------------- |
| `STEP_THRESHOLD_W`    | 50      | Minimum step size to call something an event. Raise to reduce noise; lower to catch small loads (LEDs). |
| `STEP_WINDOW_S`       | 3       | Width of the "before" and "after" windows used to measure a step. |
| `SETTLE_WINDOW_S`     | 5       | Required quiet period between events. Raise if you get duplicate events from one switch. |
| `CLUSTER_EPS_W`       | 25      | DBSCAN neighbourhood radius. Raise to merge clusters; lower to split them. |
| `CLUSTER_MIN_SAMPLES` | 5       | Minimum events needed to form a cluster. Raise to be stricter. |
| `CLUSTER_INTERVAL_S`  | 3600    | How often re-clustering runs. You can also click *Re-run clustering* in the UI. |

You won't have meaningful clusters on day one. Realistic expectations:

- **Day 1**: live monitoring works, raw events show up.
- **Week 1**: a few clusters form for high-power things you use often
  (HVAC, dryer, well pump, water heater).
- **Month 1+**: clusters become tight; matching is reliable; you've labelled
  most of the regular loads.

Things the system handles well: distinctive, repeatable loads (motors,
resistive heating elements). Things it struggles with: continuously variable
loads (modern variable-speed HVAC, dimmers, EV charging at variable rates),
small loads near the threshold, multiple devices switched at the exact same
second.

## Home Assistant integration

Set `MQTT_HOST` (and creds, if needed) in `docker-compose.yml` and restart.
The container publishes Home Assistant MQTT-discovery configs on connect:

- **Sensors** (always created): total power, total current, per-channel power
  and current.
- **Binary sensors** (created per labelled device): one `binary_sensor` per
  device, on when the matcher thinks that device is running.

Devices appear under a single HA device card named "Shelly 3EM Smart Monitor".
Power sensors are tagged with `device_class: power` and `state_class:
measurement`, so they slot into the HA Energy dashboard if you point it at
them.

If you also have the official Shelly HA integration installed, you can use it
side-by-side — it gives you the device's own diagnostic entities, while this
container adds the device-detection layer on top.

## API

The dashboard uses these endpoints; you can hit them directly too:

```
GET    /api/live                              latest sample
GET    /api/history?minutes=60                downsampled history
GET    /api/stats                             system counters
GET    /api/clusters?unlabeled_only=true      cluster list
GET    /api/clusters/{id}/events              recent events in a cluster
POST   /api/clusters/{id}/assign              {device_id} — attach cluster to existing device
POST   /api/recluster                         force a re-cluster
GET    /api/devices                           labelled devices
POST   /api/devices                           {name, notes, cluster_id} — create + label
PATCH  /api/devices/{id}                      {name?, notes?}
DELETE /api/devices/{id}                      delete (cluster becomes unlabeled)
```

## Calibration tips

1. **Turn on appliances solo when you can.** If you can switch on just one big
   appliance (well pump, water heater) with the rest of the house quiet, the
   resulting step will be clean and the cluster will form faster. Doing this
   once for each appliance shortcuts weeks of passive learning.
2. **Re-cluster after labelling sessions.** Click the button — it's cheap.
3. **Watch the live chart while flipping breakers.** That's the fastest way to
   sanity-check your channel mapping.
4. **If a cluster is two appliances**, lower `CLUSTER_EPS_W` and re-cluster.
   If two clusters should be one, raise it.

## Troubleshooting

- **No samples / "disconnected" indicator**: confirm the Shelly's IP and that
  port 80/RPC is reachable. The Shelly websocket is at `ws://<host>/rpc`. Some
  firmware versions restrict the websocket if RPC auth is enabled — disable
  device auth or extend `shelly_client.py` to send the auth handshake.
- **MQTT not connecting**: container logs show paho's connect rc code. `rc=5`
  means bad credentials. Make sure the broker allows the configured username.
- **Clusters never form**: check `/api/stats` — if `events` is zero, the
  step detector isn't triggering. Lower `STEP_THRESHOLD_W` or flip a 1500 W
  load (kettle, space heater) to seed a clear event.
- **Too many tiny clusters**: raise `CLUSTER_EPS_W` (e.g. 50) and re-cluster.
- **Container restart loses learning?** No — `./data/shelly.db` persists. If
  you blow it away you lose history, labels, and devices.

## Limitations / honest caveats

- This is *event-based* NILM, not waveform-based. It can't tell two appliances
  apart that have identical step signatures (two 1500 W heaters look the
  same). Power-factor and per-leg signatures help, but won't always disambiguate.
- The Pro 3EM samples at ~1 Hz. Sub-second start-up transients (the inrush
  current of a compressor) aren't captured. This is a hardware limit, not
  fixable in software.
- The matcher is greedy nearest-neighbour. It can mis-attribute when two
  devices switch within the same second. Real NILM systems use HMMs to handle
  this; that would be a worthwhile follow-up if accuracy matters.
