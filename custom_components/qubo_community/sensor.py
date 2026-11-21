"""Qubo sensor platform for Home Assistant."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

# Map the JSON keys to Home Assistant's built-in sensor architectures
SENSOR_TYPES = [
    {
        "key": "power",
        "name": "Power",
        "device_class": SensorDeviceClass.POWER,
        "unit": UnitOfPower.WATT,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    {
        "key": "current",
        "name": "Current",
        "device_class": SensorDeviceClass.CURRENT,
        "unit": UnitOfElectricCurrent.MILLIAMPERE,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    {
        "key": "voltage",
        "name": "Voltage",
        "device_class": SensorDeviceClass.VOLTAGE,
        "unit": UnitOfElectricPotential.VOLT,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    {
        "key": "consumption",
        "name": "Consumption Today",
        "device_class": SensorDeviceClass.ENERGY,
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    {
        "key": "duration",
        "name": "Time On Today",
        "device_class": SensorDeviceClass.DURATION,
        "unit": UnitOfTime.MINUTES,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
]


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up the sensor platform."""
    hubs = hass.data[DOMAIN][entry.entry_id]

    entities = [QuboSensor(hub, description) for hub in hubs for description in SENSOR_TYPES]
    async_add_entities(entities)


class QuboSensor(SensorEntity):
    """Representation of a Qubo Metric."""

    def __init__(self, hub, description) -> None:
        """Initialize the sensor with hub and description."""
        self._hub = hub
        self._key = description["key"]

        self._attr_name = f"{hub.device_name} {description['name']}"
        self._attr_unique_id = f"{hub.device_uuid}_{self._key}"
        self._attr_device_class = description["device_class"]
        self._attr_native_unit_of_measurement = description["unit"]
        self._attr_state_class = description["state_class"]

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the sensor."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._hub.device_uuid)},
            name=self._hub.device_name,
            manufacturer="Qubo",
            model="Smart Plug",
        )

    @property
    def native_value(self):
        """Return the state of the sensor."""
        return self._hub.metrics.get(self._key)

    @property
    def should_poll(self) -> bool:
        """Return False as updates are handled via MQTT callbacks."""
        return False

    @property
    def available(self) -> bool:
        """Return True if the plug is online."""
        return self._hub.available

    async def async_added_to_hass(self):
        """Listen for MQTT updates from the Hub."""
        self._hub.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self):
        """Disconnect MQTT callbacks when the entity is removed."""
        self._hub.unregister_callback(self.async_write_ha_state)
