"""Daemon-facing read models (device list, status) and the master-control request model."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DeviceVersion(BaseModel):
    hw_id: int
    dsp_version: int
    serial: int
    fw_major: int
    fw_minor: int

    @property
    def fw(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}"


class DaemonDevice(BaseModel):
    """One entry of ``GET /devices``. ``version`` is None while the daemon is still probing the device."""

    url: str
    product_name: str | None = None
    version: DeviceVersion | None = None

    @property
    def serial(self) -> int | None:
        return self.version.serial if self.version else None


class MasterStatus(BaseModel):
    """Global (non-preset) settings. Fields are None when unknown or not part of a partial update."""

    preset: int | None = None
    source: str | None = None
    volume: float | None = None
    mute: bool | None = None
    dirac: bool | None = None

    @field_validator("volume")
    @classmethod
    def _no_negative_zero(cls, value: float | None) -> float | None:
        # The daemon reports 0 dB as -0.0, which would show as "-0.0" in the UI, the event log and Home Assistant.
        return value + 0.0 if value is not None else None

    def merged(self, update: "MasterStatus") -> "MasterStatus":
        """Return a copy with every non-None field of ``update`` applied."""
        return self.model_copy(update=update.model_dump(exclude_none=True))


class StatusSummary(BaseModel):
    """``GET /devices/{i}`` and each upstream websocket message. Level lists are empty when not included."""

    master: MasterStatus = Field(default_factory=MasterStatus)
    input_levels: list[float] = Field(default_factory=list)
    output_levels: list[float] = Field(default_factory=list)


class MasterUpdate(BaseModel):
    """Request body for changing master controls. Only the fields that are set are applied."""

    model_config = ConfigDict(extra="forbid")

    volume: float | None = None
    mute: bool | None = None
    source: str | None = None
    preset: int | None = None
    confirm: bool = Field(
        default=False, description="Allow a guarded change, e.g. raising the volume by more than the confirm step"
    )
