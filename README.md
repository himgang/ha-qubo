# Qubo Smart Plug Community for Home Assistant

A custom integration for Home Assistant that allows you to control and monitor your Hero Electronix **Qubo Smart Plug 10A** via their cloud API.

This community-maintained fork is based on [Anonymous-AAA/ha-qubo](https://github.com/Anonymous-AAA/ha-qubo), originally created by Alen Antony. It retains the original MIT license and copyright notice in [LICENSE](LICENSE).

This integration connects directly to Qubo's cloud servers and establishes a persistent MQTT connection to give you instant switch control and real-time energy metrics.

## ✨ Features
* **UI Setup:** Fully configure the integration via the Home Assistant UI (Config Flow). No `configuration.yaml` editing required!
* **Instant Control:** Toggle your smart plug on and off instantly.
* **Energy Monitoring:** Live tracking of Power, Current, Voltage, and total energy consumption.
* **Session management:** Email and phone sessions can refresh in the background; rejected or older phone sessions require another SMS code.
* **Auto-Discovery:** Automatically discovers supported plugs and uses their Qubo names in Home Assistant.

## 🔌 Supported Devices
* Qubo Smart Plug 10A (Model: HSP02 / HSP02A)

*(Note: While built specifically for the 10A plug, it may work with the 16A variant, but this is untested.)*

## 📦 Installation

### Method 1: HACS (Recommended)
This is the easiest way to install and keep the integration updated.

1. Open Home Assistant and navigate to **HACS**.
2. Go to **Integrations**, click the three dots in the top right corner, and select **Custom repositories**.
3. Paste the URL of this repository: [https://github.com/himgang/ha-qubo](https://github.com/himgang/ha-qubo)
4. Select **Integration** as the category and click **Add**.
5. Click on the newly added **Qubo Smart Plug Community** integration and click **Download**.
6. Restart Home Assistant.

This fork and the original both use the `qubo` integration domain and install to the same directory. They cannot be installed independently side by side. Back up Home Assistant before switching, and avoid tracking both repositories in HACS to prevent one version overwriting the other.

### Method 2: Manual Installation
1. Download the latest release from this repository.
2. Extract the zip file.
3. Copy the entire `custom_components/qubo` directory into your Home Assistant's `config/custom_components/` directory.
4. Restart Home Assistant.

## ⚙️ Configuration

1. In the Home Assistant UI, go to **Settings** -> **Devices & Services**.
2. Click the **+ Add Integration** button in the bottom right corner.
3. Search for **Qubo Smart Plug Community**.
4. Choose **password** or **phone**. For password, enter your Qubo account email and password. For phone, enter the digits and country calling code (for example `+91`), then enter the SMS code in Home Assistant. If several accounts match, select the account that owns your plug.
5. The integration will discover the supported plugs on the selected account and create their entities.

**Phone sessions:** The integration exchanges the OTP result for a renewable session and stores the resulting access and refresh tokens in Home Assistant's configuration. Existing phone entries created before this change must complete SMS reauthentication to obtain a refresh token. If refresh credentials are rejected, another SMS login is required. This uses unofficial Qubo endpoints and the exchange has not yet been validated against a live account. Do not share one-time codes or session tokens; protect your Home Assistant configuration and backups.

## 📊 Entities Created
Once configured, the integration will create a Home Assistant device for each discovered plug, with the following entities:

| Entity Type | Description | Unit |
| :--- | :--- | :--- |
| `switch` | Toggles the plug On/Off | - |
| `sensor` | Current Power Draw | `W` |
| `sensor` | Electrical Current | `mA` |
| `sensor` | Electrical Voltage | `V` |
| `sensor` | Energy Consumption Today | `kWh` |
| `sensor` | Time On Today | `m` |

*Metrics are pushed from the Qubo cloud every 5 seconds while the plug is active.*

## ⚠️ Disclaimer
This is a custom, unofficial integration. It is not affiliated with, endorsed by, or supported by Hero Electronix or Qubo. It relies on a reverse-engineered cloud API. Because of this, Qubo could change their API structure at any time, which may cause this integration to temporarily or permanently stop working. Use at your own risk!
