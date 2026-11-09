# ups_monitor

Raspberry Pi service that watches a UniFi UPS Tower over NUT, shows its history in a read-only web UI, emails alerts,
reports to Uptime Kuma, and shuts down the other LAN machines over SSH (then itself) when the UPS is on battery and the
estimated runtime falls to a configured number of minutes.

- Settings: `config.yaml` (tunables) and `.env` (secrets). Constants live in `app/settings.py`.
- Two services: `ups-monitor` (polling, alerts, shutdown) and `ups-web` (read-only UI on `web.port`, default 8080).
- Everything the app owns lives in this directory: code, `.venv`, `config.yaml`, `.env`, `data/ups.db`, `keys/`.

## Development

```bash
./setup.sh --dev                                  # first-time setup: venv, dependencies (incl. pytest), config.yaml and .env from the examples, SSH key, empty database
.venv/bin/python -m pytest -q                     # run the test suite
.venv/bin/python -m app config                    # validate config.yaml and the .env variables it needs
.venv/bin/python -m app initdb                    # create or upgrade the SQLite database schema
.venv/bin/python -m app poll                      # read the UPS once and print every variable; stores nothing
.venv/bin/python -m app monitor                   # run the daemon: polling, events, alerts, Kuma heartbeat, shutdown logic, retention
.venv/bin/python -m app web --dev                 # serve the read-only web UI with Flask's dev server (production uses gunicorn via `app web`)
.venv/bin/python -m app check                     # full readiness check of every target (ping, ssh login, sudo rule, poweroff dry-run); powers nothing off; results appear on the dashboard
.venv/bin/python -m app check --trust-new-hosts   # same, and pin host keys of machines not yet in keys/known_hosts (first time only, trusted LAN)
.venv/bin/python -m app drill                     # rehearse the whole shutdown sequence as a dry run; real emails and history, nothing powered off
.venv/bin/python -m app testmail                  # send one test email using the smtp settings
.venv/bin/python -m app testkuma                  # send one test heartbeat to the Uptime Kuma push monitor
.venv/bin/python -m app retention                 # run the rollup/purge of old data once now (the daemon also does this hourly)
```

## Installing on the production Pi

1. Copy the project directory over, leaving out `.venv/`, `data/`, `keys/` and `.env`, and run `./setup.sh` there.
   Every installation makes its own SSH key; do not copy the dev key. The script is safe to re-run and never
   overwrites existing files. It:
   - checks prerequisites (Python 3.11+, `python3-venv`, `openssh-client`, `iputils-ping`) and prints the `apt` command if any are missing;
   - creates `.venv` and installs `requirements.txt` (`--dev` adds `requirements-dev.txt`);
   - creates `data/` and `keys/`, and `.env` and `config.yaml` from the examples if absent;
   - generates the SSH key `keys/ups_shutdown` and prints its public key, which the target machines need;
   - adds `.env`, `keys/`, `data/`, `.venv/` and `config.yaml` to `.gitignore`;
   - creates the empty database (`app initdb`).
2. Edit `.env` (SMTP and Kuma secrets) and `config.yaml`. Keep `shutdown.dry_run: true` for now.
   Check with `.venv/bin/python -m app config`, then `testmail` and `testkuma`.
3. Prepare every target machine (next section), then pin their host keys and verify:
   ```bash
   .venv/bin/python -m app check --trust-new-hosts   # first time only, on a trusted LAN
   .venv/bin/python -m app check                     # must report OK for every target and this host
   ```
4. Install the services (the only step that needs root; it also adds the sudo rule for this host's own poweroff):
   ```bash
   sudo deploy/install.sh              # uses the user that ran sudo; --user NAME to choose another
   systemctl status ups-monitor ups-web
   ```
   `deploy/install.sh --render-only DIR` writes the unit files to `DIR` without installing anything.
5. In Uptime Kuma, point a Push monitor at `KUMA_PUSH_URL` with a heartbeat interval above `kuma.interval_s`. Optionally
   add an HTTP monitor for `http://<pi>:8080/healthz` (200 while the monitor is polling).
6. Restart after changing `config.yaml`: `sudo systemctl restart ups-monitor ups-web`.

## Preparing a target machine

Each target needs a locked-down user, the monitor's public key, and a sudo rule for one command. On the monitor:

```bash
cat keys/ups_shutdown.pub          # the public key
```

Copy `deploy/setup-target.sh` to the target and run it there as root, giving the monitor's IP so the key only works
from that address:

```bash
sudo ./setup-target.sh --pubkey "ssh-ed25519 AAAA... ups_monitor@pi5-1" --from 10.10.1.50
```

It creates `upsshutdown` (no password), installs the key as `restrict,from="..."` and adds
`/etc/sudoers.d/ups-shutdown` allowing only `/usr/bin/systemctl poweroff` and its `--dry-run` form (used by the readiness check). `--dry-run` shows the changes without making
them. The target must also run `sshd` and answer ping, because the monitor uses ping to confirm a machine is down.

## Shutdown readiness

The dashboard's **Shutdown readiness** panel shows, per machine in shutdown order, the last result of ping, SSH login
(pinned host key, key auth), the sudo rule, and a `systemctl poweroff --dry-run` through sudo, with the time of the check.
Everything is verified except the actual power-off, which only a manual live test can prove.

- The monitor runs the check 60 s after it starts and then every `shutdown.preflight_interval_hours` (default 24).
- **Check now** runs it immediately: the web service drops a request file in `data/` and the monitor picks it up within
  a couple of seconds. It needs the monitor running, and is the only action the web UI can perform; set
  `web.allow_check_now: false` to remove it. `python -m app check` also updates the panel.
- A target that has not been checked in twice the interval shows as *not verified*. Failures are emailed
  (`preflight_failed`); pressing the button repeatedly does not repeat the email.
- *Partial* means everything required passed but the dry-run is not enabled on that target: re-run `setup-target.sh`
  there once (for "this host", run `sudo deploy/install.sh`, which also adds the local sudoers rule).

## Rehearsing, then going live

1. `.venv/bin/python -m app drill` runs the whole sequence as a dry run: events, emails and the dashboard's shutdown history are
   real, nothing is powered off.
2. Optional outage test with `dry_run: true`: set `runtime_threshold_minutes` above the UPS's current runtime, unplug the
   UPS from the wall for a minute and watch the log, emails and dashboard. Plug it back in; the sequence aborts if it has
   not started shutting machines down.
3. Live test on one non-critical machine: temporarily list only that machine, set `shutdown_self: false` and
   `dry_run: false`, repeat the unplug test, then restore your real settings.
4. Set `dry_run: false` in `config.yaml` and restart `ups-monitor`.

If the monitor restarts during a sequence, the interrupted run is closed and a new one starts if the condition persists;
machines that are already down are skipped.

## Operations

Manage the services with `systemctl` (`ups-monitor` is the daemon, `ups-web` is the dashboard):

```bash
systemctl status ups-monitor ups-web             # state of both services
sudo systemctl start ups-monitor                 # start (same for ups-web)
sudo systemctl stop ups-monitor                  # stop; the UPS is no longer watched and no shutdown can happen
sudo systemctl restart ups-monitor ups-web       # restart both, e.g. after editing config.yaml or .env
sudo systemctl reload-or-restart ups-web         # reload if supported, otherwise restart
sudo systemctl enable ups-monitor ups-web        # start at boot (install.sh already does this)
sudo systemctl disable ups-monitor ups-web       # do not start at boot
sudo systemctl enable --now ups-monitor          # enable and start in one step
systemctl is-active ups-monitor ups-web          # prints active/inactive for scripts
systemctl is-enabled ups-monitor ups-web         # prints enabled/disabled
sudo systemctl reset-failed ups-monitor          # clear a failed state after fixing the cause
```

Stopping `ups-web` only removes the dashboard; the monitor keeps running independently.

- Logs: `journalctl -u ups-monitor -f` and `journalctl -u ups-web`.
- Data: `data/ups.db` (SQLite, WAL). Raw samples are kept `retention.raw_days`, 1-minute averages `retention.rollup_days`,
  events `retention.events_days` and shutdown history `retention.shutdown_days` (all default 365). Back up with `sqlite3 data/ups.db ".backup 'ups-backup.db'"`.
- The NUT server has a client limit, so the monitor holds one short connection per poll and never more.
- Never run a real poweroff while developing. Tests block all process launches, and the drill forces dry run.
