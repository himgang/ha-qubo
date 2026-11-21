"""Qubo Smart Plug Switch Entity."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: callable):
    """Set up the Qubo switch platform from a config entry."""
    hubs = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([QuboSwitch(hub) for hub in hubs])

class QuboSwitch(SwitchEntity):
    """Representation of a Qubo smart plug switch."""

    def __init__(self, hub) -> None:
        """Initialize the Qubo switch entity."""
        self._hub = hub
        self._attr_name = hub.device_name
        self._attr_unique_id = hub.device_uuid
        self._attr_icon = "mdi:power-plug"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the switch."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._hub.device_uuid)},
            name=self._hub.device_name,
            manufacturer="Qubo",
            model="Smart Plug",
        )

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        return self._hub.state

    @property
    def should_poll(self) -> bool:
        """Return whether polling is required for this entity."""
        return False

    @property
    def available(self) -> bool:
        """Return True if the plug is online."""
        return self._hub.available


    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        await self._hub.turn_on()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        await self._hub.turn_off()

    async def async_added_to_hass(self):
        """Register the hub callback when the entity is added."""
        self._hub.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self):
        """Unregister the hub callback when the entity is removed."""
        self._hub.unregister_callback(self.async_write_ha_state)
