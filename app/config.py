from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every setting comes from an environment variable of the same name (upper-case)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Daemon / server
    minidspd_url: str = "http://127.0.0.1:5380"
    daemon_timeout_s: float = 5.0
    webui_bind: str = "0.0.0.0"
    webui_port: int = 8480
    db_path: str = "/data/minidsp-webui.sqlite3"
    log_level: str = "info"
    public_url: str = ""  # used for links back to the UI (e.g. HA configuration_url)

    # Sync behaviour
    write_coalesce_ms: int = 50
    reapply_on_reconnect: bool = False
    mute_wrap_bulk_apply: bool = True
    preset_switch_timeout_s: float = 15.0

    # Safety limits for channel / routing gains (dB). Configurable, but only inside what the hardware handles safely:
    # on a 2x4HD gains below about -100 dB read back as +6.3 dB (possibly a real wraparound) and +24 dB and up read 0.
    gain_min_db: float = Field(default=-72.0, ge=-100.0, lt=24.0)
    gain_max_db: float = Field(default=12.0, ge=-100.0, lt=24.0)
    gain_step_confirm_db: float = 6.0

    # Monitor
    monitor_backoff_min_s: float = 0.5
    monitor_backoff_max_s: float = 10.0
    levels_idle_grace_s: float = 5.0
    discovery_interval_s: float = 30.0

    # MQTT / Home Assistant discovery
    mqtt_enabled: bool = False
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = Field(default="", repr=False)
    mqtt_discovery_prefix: str = "homeassistant"
    mqtt_base_topic: str = "minidsp-webui"
    mqtt_volume_floor_db: float = -127.0

    @model_validator(mode="after")
    def _check_gain_range(self) -> "Settings":
        if self.gain_min_db >= self.gain_max_db:
            raise ValueError(f"GAIN_MIN_DB ({self.gain_min_db}) must be below GAIN_MAX_DB ({self.gain_max_db})")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
