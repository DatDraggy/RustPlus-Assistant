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

Once installed, configure the integration via the Home Assistant UI:

1. Go to **Settings** > **Devices & Services**.
2. Click **Add Integration** in the bottom right.
3. Search for **Rust+ Assistant** and select it.
4. Follow the setup prompts:
   - **FCM Credentials**: Provide the FCM credentials JSON string you get from https://chromewebstore.google.com/detail/rustpluspy-link-companion/gojhnmnggbnflhdcpcemeahejhcimnlf?hl=en.

### Pairing Devices

When you pair a new device (like a Smart Switch, Smart Alarm, or Storage Monitor) in the Rust+ app:
1. Home Assistant will detect the pairing.
2. An alert/notification will appear in your Home Assistant dashboard notifying you that a device has been paired.
4. After pairing, rename the entity in your Home Assistant Device page to give it a clean, user-friendly name.

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.
