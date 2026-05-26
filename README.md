# Shelly 3EM Smart Monitor — Home Assistant Add-on Repository

A Home Assistant add-on (and standalone Docker image) that streams data from a
Shelly Pro 3EM, auto-detects connected appliances from their power signatures,
and exposes them inside Home Assistant.

## Install as a Home Assistant add-on

> Requires Home Assistant **OS** or **Supervised**. (For HA Container/Core, see
> the standalone Docker setup below.)

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu in the top-right → **Repositories**.
3. Paste the URL of this repository and click **Add**:
   ```
   https://github.com/begleyt/shelly-3em-smart
   ```
4. Close the dialog and scroll to the bottom — the **Shelly 3EM Smart Monitor**
   add-on now appears. Click **Install**.
5. On the **Configuration** tab, set `shelly_host` to the IP of your Pro 3EM,
   then **Start** the add-on.
6. Open the add-on's **Web UI** button — the dashboard appears as a sidebar
   panel inside Home Assistant (via ingress).

If you have the **Mosquitto broker** add-on installed, the add-on auto-detects
it and publishes every labelled appliance as a `binary_sensor`, plus per-leg
power/current sensors, via Home Assistant's MQTT discovery — no extra
configuration needed.

## What's in this repository

```
shelly-3em-smart/         # the add-on itself
├── config.yaml           # add-on manifest (options schema, ports, ingress)
├── build.yaml            # multi-arch base images
├── Dockerfile            # used by HA Supervisor (and CI)
├── Dockerfile.standalone # used by docker-compose (no HA dependency)
├── docker-compose.yml    # for running outside HA
├── run.sh                # add-on entrypoint (bashio → env vars)
├── app/                  # FastAPI + clusterer + WebSocket client
└── README.md             # detailed docs (tuning, calibration, API)

.github/workflows/        # CI that publishes multi-arch images to ghcr.io
repository.yaml           # marks this repo as an HA add-on repository
```

## Standalone (without Home Assistant)

```sh
cd shelly-3em-smart
# Edit docker-compose.yml: set SHELLY_HOST
docker compose up -d --build
# Open http://localhost:8080
```

See [shelly-3em-smart/README.md](shelly-3em-smart/README.md) for the full
documentation — environment variables, tuning, the REST API, and honest
caveats about what the inference can and can't do.

## How it works (one paragraph)

The add-on keeps a websocket open to the Shelly's RPC port and receives per-leg
voltage / current / power / power-factor samples about once a second. It
watches for clean step changes in total active power and emits an `on` or `off`
event whenever the change is larger than a configurable threshold. Once an
hour, DBSCAN clusters the recorded events into groups of similar signatures —
each cluster is a probable appliance. You label them once from the dashboard,
and from then on the matcher tags each new event with the right device and
publishes its state via MQTT.

It needs time to learn (typically a few days to a couple of weeks for
infrequent appliances) and works best on distinctive, repeatable loads — a
well pump, dryer, electric water heater, HVAC compressor. It will struggle
with continuously-variable loads like modern variable-speed HVAC, dimmers,
and EV chargers.

## License

MIT
