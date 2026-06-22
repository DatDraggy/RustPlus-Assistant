# Rust+ Assistant for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Home Assistant Minimum Version](https://img.shields.io/badge/Home%20Assistant-2026.6.4%2B-blue.svg?style=for-the-badge)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

A Home Assistant custom integration to connect with the **Rust+ Companion App** API. It allows you to monitor and control your in-game Rust entities directly from Home Assistant.

---

## Features

- 🔌 **Smart Switches**: Turn your in-game smart switches on and off from Home Assistant.
- 🚨 **Smart Alarms**: Receive binary sensor alerts for base alarms
- 📦 **Storage Monitors**: Monitor inventory levels of chests and storage boxes (e.g., amount of sulfur, metal, or wood).
- 👥 **Server Stats**: Monitor online player count, queue size, and max player capacity.
- 🗺️ **Map Camera**: View the in-game map as a camera entity in Home Assistant.

---

## Companion Dashboard Card

For a game-style visual grid of your Storage Monitors (item stacks plus a color-coded decay/upkeep timer), install the companion Lovelace card:

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

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Disclaimer

This code is heavily based on Gemini and Claude's work, but manually reviewed and tested.