# AGENT.md — ups_monitor

A small service for a Raspberry Pi that (1) watches the NUT server on a UniFi UPS Tower,
(2) shows the UPS's data over time in a **read-only** web UI, and (3) shuts down the other
machines on the LAN over SSH, in a configurable order, then itself, when the UPS is on battery and
the estimated runtime falls to a configurable number of minutes. It also sends email alerts and can
be monitored by Uptime Kuma. **All configuration lives in one file, `config.yaml`.**

This file is the plan and the rules for the coding agent. Read all of it before writing code.
Build in the milestone order at the end, and stop at the end of each milestone.

---

## 1. Decisions already made by the owner

| Topic | Decision |
|---|---|
| Host | Raspberry Pi with **NVMe drive** (not an SD card). It also acts as an app server that runs other apps. |
| Storage | **SQLite**, stored **locally inside the app's own directory tree**. No NAS dependency. |
| Configuration | The web UI has **no configuration ability** other than light and dark mode theme. |
| Web access | LAN only, no login, **no allowed-networks list**. |
| Shutdown order | **Configurable in `config.yaml`**, with no limit on the number of machines. See section 7. |
| Notifications | **SMTP email.** |
| Monitoring | **Uptime Kuma** must be able to monitor the app. |

---

## 2. Environment and hard constraints

| Item | Value |
|---|---|
| UPS | Ubiquiti `TOWER_1000VA_120V`, line-interactive, no USB port, hosts the NUT server |
| NUT server | `10.10.1.100:3493`, UPS name `ups1`, **TLS (STARTTLS), self-signed cert** |
| NUT client cap | **3 clients max** on the UPS's NUT server. See the slot budget below. |
| Host | Raspberry Pi 5 with NVMe, Debian-based OS, no Docker |
| Shutdown targets | Linux machines with SSH (P3Tiny, pi4-1, and more), then the host itself |
| Timezone | America/New_York for display |

**Slot budget (read carefully).** The UPS' NUT server allows 3 NUT connections in total. Probably in use today
(this is an inference, not verified).

- **Use at most one connection at a time**
- If a poll is refused because the UPS is full, treat it exactly like any other failed poll: a gap,
  never a trigger. Log "connection refused (UPS may be at its client limit)" as its own event type.

Hard constraints:

- **No Docker.** Native Python in a venv, run by systemd.
- **Self-contained.** Everything the app owns (code, venv, config, database, keys) lives under one
  directory. See section 14.
- If the database, SMTP, Kuma, or the web UI fail, the monitor and the shutdown logic keep working.
- **Works without internet or CDNs.** Vendor all JS and CSS locally. The UI must work during an
  outage.
- **LAN only.** No cloud calls. (SMTP goes to whatever server the owner configures.)
- **Never run a real `poweroff` in tests or in development.** See section 10.

---

## 3. What the UPS exposes

Observed over NUT (values are examples from a healthy, idle UPS):

| Variable | Example | Used for |
|---|---|---|
| `battery.charge` | 100 | Battery charge % |
| `battery.voltage` | 13.7 | Battery voltage |
| `battery.runtime` | 2389 | Runtime to empty (seconds) |
| `battery.low` | 9.67 | Display only, raw, units unknown |
| `input.voltage` | 122.3 | Real-time input voltage |
| `input.voltage.nominal` | 120.0 | Nominal input voltage |
| `input.transfer.high` | 145.0 | High voltage transfer threshold |
| `input.transfer.low` | 89.0 | Low voltage transfer threshold |
| `input.frequency` | 60.0 | Extra |
| `output.voltage` | 122.3 | Real-time output voltage |
| `output.current` | 1.20 | Output current (A) |
| `output.frequency` | 60.0 | Extra |
| `ups.realpower` | 84.0 | Real-time output power (W) |
| `ups.realpower.nominal` | 600.0 | Nominal output power (W) |
| `ups.power` | 146.8 | Real-time output power (VA) |
| `ups.power.nominal` | 1000 | Nominal output power (VA) |
| `ups.load` | 14 | Extra (%) |
| `ups.status` | `OL` | UPS status flags |
| `ups.serial` | 1C0B8B3A6FF7 | Serial number |
| `ups.type` | line-interactive | UPS type |
| `ups.model`, `ups.mfr` | TOWER_1000VA_120V, Ubiquiti | Header |
| `ups.test.result` | done and passed | Self-test result |
| `ups.test.date` | 2026-10-28 | Self-test date (see quirk below) |
| `ups.test.interval` | 604800 | Self-test interval (s) |
| `ups.temperature` | 25.0 | Extra (C) |

Known UPS behavior (build around it):

- **Weekly self-test.** Every Tuesday evening (about 22:40 to 22:55 local, drifting a few minutes
  later each week) the status goes `OB CAL` for about 10 seconds, then `OL`. Seen Oct 6, 13, 20, 27.
- **After a test the charge can fall and refill slowly.** On Oct 20 the charge read 60% right
  after the test and took about 3.3 hours to return to 100%. Do not treat that as a fault.
- **`ups.test.date` is a UTC date.** A test at 22:52 local on Oct 27 reads `2026-10-28`. Show the
  value as reported, and record real test events from status transitions with local timestamps.
- **The UPS may never set `LB`.** Not confirmed either way.

---
