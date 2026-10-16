"""Static capabilities of a supported device model.

Counts and ranges come from `DeviceProfile`, never from literals scattered through the app.
"""

from pydantic import BaseModel, ConfigDict


class DeviceProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    product_name: str
    n_inputs: int
    n_outputs: int
    n_presets: int = 4
    peq_per_input: int = 10
    peq_per_output: int = 10
    crossover_groups: int = 2
    crossover_biquads: int = 4
    sources: tuple[str, ...]
    sample_rate: int = 96_000
    delay_max_samples: int = 8000
    fir_max_taps: int = 4096
    fir_supported: bool = False
    dirac_supported: bool = False
    master_volume_min_db: float = -127.0
    master_volume_max_db: float = 0.0
    master_volume_step_db: float = 0.5
    gain_min_db: float = -72.0
    gain_max_db: float = 12.0

    @property
    def delay_max_ms(self) -> float:
        return self.delay_max_samples / self.sample_rate * 1000.0

    def delay_samples(self, delay_ms: float) -> int:
        """Delay in whole samples, as the device stores it."""
        return round(delay_ms * self.sample_rate / 1000.0)

    def delay_ms_from_samples(self, samples: int) -> float:
        return samples * 1000.0 / self.sample_rate

    def canonical_source(self, name: str) -> str | None:
        """Case-insensitive lookup of a source name; returns the canonical spelling or None."""
        lowered = name.strip().lower()
        return next((s for s in self.sources if s.lower() == lowered), None)


M2X4HD = DeviceProfile(
    product_name="2x4HD",
    n_inputs=2,
    n_outputs=4,
    sources=("Analog", "Toslink", "Usb"),
    # Measured on hardware: the device holds at most 7664 samples (79.833 ms). minidsp-rs accepts up to 8000, but
    # 7665..8000 answer 200 and are silently clamped to 7664.
    delay_max_samples=7664,
)

PROFILES: dict[str, DeviceProfile] = {M2X4HD.product_name: M2X4HD}


def profile_for(
    product_name: str | None,
    *,
    gain_min_db: float | None = None,
    gain_max_db: float | None = None,
    fir_supported: bool | None = None,
    dirac_supported: bool | None = None,
) -> DeviceProfile | None:
    """Profile for a daemon `product_name`, with optional overrides. None when the model is unsupported."""
    if product_name is None:
        return None
    base = PROFILES.get(product_name)
    if base is None:
        return None
    overrides = {
        k: v
        for k, v in {
            "gain_min_db": gain_min_db,
            "gain_max_db": gain_max_db,
            "fir_supported": fir_supported,
            "dirac_supported": dirac_supported,
        }.items()
        if v is not None
    }
    return base.model_copy(update=overrides) if overrides else base
