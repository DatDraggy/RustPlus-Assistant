# Rust+ Assistant for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Home Assistant Minimum Version](https://img.shields.io/badge/Home%20Assistant-2026.6.4%2B-blue.svg?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

A Home Assistant custom integration to connect with the **Rust+ Companion App** API. It allows you to monitor and control your in-game Rust entities directly from Home Assistant.

---

## Features

- 🔌 **Smart Switches**: Turn your in-game smart switches on and off from Home Assistant.
- 🚨 **Smart Alarms**: Binary sensor + event entities per alarm, driven live over the websocket.
- 📦 **Storage Monitors**: Inventory levels of TCs and boxes, with per-material sensors and a TC upkeep countdown.
- 👥 **Server & Team**: Player counts/queue, server info (map/seed/wipe), in-game clock & daytime, team size, and a **sensor per teammate** (alive/dead/offline, map grid position, leader).
- 🗺️ **Map Cameras**: The server map, plus an annotated map with monuments, events, vending machines and team positions drawn on.
- 🎥 **CCTV & Turrets**: Live camera feeds by in-game identifier; turrets get aim buttons, a fire button and an opt-in Control switch (auto-aim stays on until you take control).
- 📅 **Event tracking**: Cargo Ship / Patrol Helicopter / CH47 / Traveling Vendor presence sensors **plus estimated next-spawn countdown sensors** (learned from observed spawn cadence).
- 💬 **Team chat**: Last-message sensor, chat events on the HA bus, `!command` events for automations, and a service to post into team chat.
- 💥 **Destroyed-device detection**: When a paired device is destroyed in-game, its entities go unavailable and a Repair offers one-click removal.
- ☠ **Death notifications**: Killed (e.g. while offline)? A prompt with the killer's name appears, plus a `rustplus_death` event for automations.
- 🛡️ **Raid alert blueprint**: Tiered raid notifications (DND-bypassing criticals, offline-team escalation, in-game chat callout) — see [Automating](#automating).

### For a visual showcase, you can check out my video: https://www.youtube.com/watch?v=ed0F8BjBLkY
---

## Companion Dashboard Cards

Game-styled Lovelace cards for everything above — storage grid, server banner, in-game clock, turret control (feed + D-pad), team squad roster, team chat, event feed and a raid-defense board:

➡️ **[DatDraggy/RustPlus-Assistant-Cards](https://github.com/DatDraggy/RustPlus-Assistant-Cards)**

---

## Installation

### Method 1: Via HACS (Recommended)

1. Open **HACS** in your Home Assistant instance.
2. Click the three dots in the top right corner and select **Custom repositories**.
3. Enter the URL of this repository: `https://github.com/DatDraggy/RustPlus-Assistant`
4. Set the category to **Integration** and click **Add**.
5. Find the **Rust+ Assistant** integration in HACS and click **Download**.
6. Restart Home Assistant.

### Method 2: Manual Installation

1. Download the latest release or clone this repository.
2. Copy the `custom_components/rustplus_assistant` folder to your Home Assistant's `config/custom_components/` directory.
3. Restart Home Assistant.

---

## Configuration

Once installed, add the integration via the Home Assistant UI:

1. Go to **Settings** > **Devices & Services**.
2. Click **Add Integration** in the bottom right, then search for **Rust+ Assistant** and select it.
3. Choose how to sign in to your Rust+ (Steam) account:

#### 🔑 Scan a Steam QR code (recommended)

No browser extension and no third-party website required — Home Assistant talks to Steam and Facepunch directly.

1. Home Assistant shows a **QR code**.
2. Open the **Steam Mobile app**, tap the QR-code scanner, scan the code, and **approve** the sign-in.
3. Click **Submit**. Home Assistant finishes signing in and registers for push notifications automatically.

> Your Steam password is never entered into — or seen by — Home Assistant. This is the same QR sign-in Steam uses on the desktop client; you approve it on your phone. Each Home Assistant install registers its own push device, so multiple instances (e.g. a test and a production server) can run in parallel without invalidating each other.

#### 📋 Paste FCM credentials JSON (fallback)

If you prefer, or if the QR sign-in isn't working for you, generate credentials with the [Rustplus.py Link Companion browser extension](https://chromewebstore.google.com/detail/rustpluspy-link-companion/gojhnmnggbnflhdcpcemeahejhcimnlf?hl=en) and paste the JSON string.

### Refreshing credentials

If your credentials ever stop working, open the **Rust+ Account** entry under **Settings** > **Devices & Services**, click the **⋮** menu > **Reconfigure**, and re-authenticate (Steam QR or paste JSON). Your paired devices and servers are kept.

### Pairing Devices

When you pair a new device (like a Smart Switch, Smart Alarm, or Storage Monitor) in the Rust+ app:
1. Home Assistant will detect the pairing.
2. An alert/notification will appear in your Home Assistant dashboard notifying you that a device has been paired.
4. After pairing, rename the entity in your Home Assistant Device page to give it a clean, user-friendly name.

### Options

On a server entry, **Configure** offers: adding/removing CCTV/turret **cameras** (by their in-game Computer Station identifier) and the team-chat **command prefix** (default `!`).

> ⚠️ **Entity IDs are server-scoped** (since v1.5.0): ids are prefixed with a short label derived from the server name — e.g. `[EU] TideRust |Solo...` yields `sensor.tiderust_time`, `camera.tiderust_map`. If you upgraded from ≤1.4.x this was a **breaking rename**; re-point any dashboards/automations that referenced old ids.

---

## Automating

### Services

| Service | Description |
| --- | --- |
| `rustplus_assistant.send_team_message` | Post a message into the in-game team chat. |
| `rustplus_assistant.promote_leader` | Promote a team member (optional `steam_id`) to team leader. |

### Events (on the HA bus)

| Event | Fired when | Notable data |
| --- | --- | --- |
| `rustplus_team_chat` | Any team-chat message arrives | `sender_name`, `sender_steam_id`, `message` |
| `rustplus_command` | A chat message starts with the command prefix (default `!`) | `command`, `args`, sender fields |
| `rustplus_team_event` | The team changes | `joined`, `left`, `came_online`, `went_offline`, `died`, leader/counts |
| `rustplus_death` | You are killed (e.g. while offline) | `killer`, `killer_steam_id`, `server_name` |
| `rustplus_notification` | Any Rust+ push arrives | `title`, `message`, `channel_id` |

Example — react to `!lights` typed in team chat:

```yaml
trigger:
  - platform: event
    event_type: rustplus_command
    event_data:
      command: lights
action:
  - service: light.toggle
    target: { entity_id: light.base_exterior }
```

### Raid alert blueprint

[`blueprints/rustplus_raid_alert.yaml`](blueprints/rustplus_raid_alert.yaml) sends **tiered raid notifications**: critical alarms always push with Do-Not-Disturb bypass, high alarms escalate to critical when **no teammate is online**, low alarms are logbook-only — with an optional callout into team chat. Copy it to `config/blueprints/automation/rustplus_assistant/` (or import the raw-file URL via **Settings → Automations → Blueprints → Import**), then create an automation from it and assign your alarms to tiers.

Tip: an in-game **Seismic Sensor** outputs 3 rW for MLRS/Rocket/C4, 2 rW for the satchel tier and 1 rW for grenades (single 3 s pulse) — branch that into three Smart Alarms and the tiers map to real explosive classes. The wiring recipe is in the [cards repo README](https://github.com/DatDraggy/RustPlus-Assistant-Cards).

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Disclaimer

This code is heavily based on Gemini and Claude's work, but manually reviewed and tested.
