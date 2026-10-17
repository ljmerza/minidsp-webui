from datetime import time
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    # Lowering the master volume by more than this in one UI action asks first (0 = never); HA/API drops aren't guarded.
    volume_drop_confirm_db: float = Field(default=20.0, ge=0.0, le=127.0)
    # Most a single PEQ band or crossover group may boost (its peak gain over 5 Hz-24 kHz); larger boosts are refused.
    peq_max_boost_db: float = Field(default=24.0, ge=0.0, le=60.0)

    # Graphs: the frequency view they open in ("auto" = each page's own: 5-300 Hz on the dashboard, crossover and
    # system pages, 10 Hz-20 kHz on PEQ and Response) and the default magnitude scale (+/- this many dB)
    graph_view: Literal["auto", "sub", "full"] = "auto"
    graph_db_range: float = Field(default=24.0, ge=3.0, le=60.0)

    # Experimental: FIR on models whose profile doesn't claim it (unverified on the 2x4HD; loading taps mutes it)
    fir_experimental: bool = False

    # Monitor
    monitor_backoff_min_s: float = 0.5
    monitor_backoff_max_s: float = 10.0
    levels_idle_grace_s: float = 5.0
    discovery_interval_s: float = 30.0

    # Readback: verify the active preset against what the app last wrote, once a night at VERIFY_AT (local time)
    verify_nightly: bool = False
    verify_at: str = "04:15"

    # MQTT / Home Assistant discovery
    mqtt_enabled: bool = False
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = Field(default="", repr=False)
    mqtt_discovery_prefix: str = "homeassistant"
    mqtt_base_topic: str = "minidsp-webui"
    mqtt_volume_floor_db: float = -127.0
    mqtt_levels: bool = False  # level sensors; feeding them keeps the device's USB level polling on

    @model_validator(mode="after")
    def _check_gain_range(self) -> "Settings":
        if self.gain_min_db >= self.gain_max_db:
            raise ValueError(f"GAIN_MIN_DB ({self.gain_min_db}) must be below GAIN_MAX_DB ({self.gain_max_db})")
        return self

    @field_validator("verify_at")
    @classmethod
    def _check_verify_at(cls, value: str) -> str:
        try:
            time.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(f"VERIFY_AT must be a local time like 04:15, got {value!r}") from None
        return value.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
