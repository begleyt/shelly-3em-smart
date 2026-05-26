# Shelly 3EM Smart Monitor

Streams data from a Shelly Pro 3EM, auto-detects connected appliances from
their power signatures, and exposes them as native Home Assistant entities.

This repository ships **two things** that work together:

| Piece | What it does | Where it runs |
| --- | --- | --- |
| **Add-on** | The workhorse — opens a websocket to the Shelly, detects power-step events, clusters them, runs the dashboard | A Docker container managed by HA Supervisor (or standalone docker-compose) |
| **HACS integration** | A thin proxy that polls the add-on's REST API and creates native HA sensors + binary_sensors | Inside Home Assistant itself |

You install both. The add-on does the heavy lifting; the integration gives you
the in-HA experience (entities, automations, the Energy dashboard, etc.) without
needing MQTT.

---

## Install — step 1: the add-on

> Requires Home Assistant **OS** or **Supervised** for the add-on. For HA
> Container/Core, run it via standalone `docker-compose` (see below) and skip
> to step 2.

1. **Settings → Add-ons → Add-on Store**
2. Top-right **⋮ → Repositories**
3. Paste: `https://github.com/begleyt/shelly-3em-smart` → **Add**
4. Close, scroll down, install **Shelly 3EM Smart Monitor**
5. **Configuration** tab → set `shelly_host` to the IP of your Pro 3EM → **Save**
6. **Start** the add-on. Open the **Web UI** button to confirm live data is
   streaming (you should see watts per leg within a couple of seconds).

## Install — step 2: the HACS integration

> Requires [HACS](https://hacs.xyz) installed (it usually is on community HA
> setups).

1. **HACS → Integrations** (sidebar)
2. Top-right **⋮ → Custom repositories**
3. URL: `https://github.com/begleyt/shelly-3em-smart`, Category: **Integration** → **Add**
4. Search for **Shelly 3EM Smart Monitor** in the HACS list → **Download**
5. Restart Home Assistant
6. **Settings → Devices & services → Add Integration** → search **Shelly 3EM
   Smart Monitor** → enter the add-on's host and port

   - **HA OS / Supervised** (add-on is `host_network: true`): host `localhost`, port `8080`
   - **HA Container / Core, add-on running on a different machine**: host = that machine's IP, port `8080`

After this you'll have a new device card under **Settings → Devices &
services → Shelly 3EM Smart Monitor** with:

- 8 numeric sensors: total power/current + per-leg power/current (and per-leg
  voltage, disabled by default — enable in the entity registry if you want them)
- One `binary_sensor` per appliance you label in the add-on dashboard, created
  automatically on the next poll after labelling

## Don't want HA at all?

```sh
cd shelly-3em-smart
# Edit docker-compose.yml: set SHELLY_HOST
docker compose up -d --build
# Open http://localhost:8080
```

The standalone Docker setup uses the same image and the same dashboard, just
without HA Supervisor managing the lifecycle. The HACS integration can still
talk to it — point the config flow at that machine's IP.

## Optional: MQTT discovery

If you'd rather have entities appear via MQTT discovery than via the HACS
integration, the add-on can do that too. **Settings → Add-ons → Shelly 3EM
Smart Monitor → Configuration**: toggle `mqtt_enabled: true` and either leave
`mqtt_host` blank (uses HA's Mosquitto broker add-on if installed) or fill in
your external broker's details. The two paths are mutually exclusive —
**use one or the other, not both**, or you'll get duplicate entities.

The HACS integration is the recommended path because it works without an MQTT
broker, gives a cleaner device card, and exposes richer attributes on each
entity.

---

## What's in this repository

```
shelly-3em-smart/                  # the add-on
├── config.yaml                    # add-on manifest (options schema, ports, ingress)
├── Dockerfile / Dockerfile.standalone / docker-compose.yml
├── build.yaml                     # multi-arch base images
├── run.sh                         # bashio entrypoint
├── app/                           # FastAPI + clusterer + WebSocket client
└── README.md                      # detailed add-on docs (tuning, API, caveats)

custom_components/shelly_3em_smart/  # the HACS integration
├── manifest.json
├── __init__.py / config_flow.py / coordinator.py
├── sensor.py / binary_sensor.py
└── strings.json / translations/

.github/workflows/                 # CI: multi-arch image publish to ghcr.io
repository.yaml                    # marks this as an HA add-on repository
hacs.json                          # marks this as an HACS integration
```

## How it works (one paragraph)

The add-on keeps a websocket open to the Shelly's RPC port and receives per-leg
voltage / current / power / power-factor samples about once a second. It
watches for clean step changes in total active power and emits an `on` or `off`
event whenever the change is larger than a configurable threshold. Once an
hour, DBSCAN clusters the recorded events into groups of similar signatures —
each cluster is a probable appliance. You label them once from the dashboard,
and from then on the matcher tags each new event with the right device. The
HACS integration polls the add-on every 2 s and reflects everything as native
HA entities.

It needs time to learn (typically a few days to a couple of weeks for
infrequent appliances) and works best on distinctive, repeatable loads — a
well pump, dryer, electric water heater, HVAC compressor. It will struggle
with continuously-variable loads like modern variable-speed HVAC, dimmers,
and EV chargers.

## License

MIT
