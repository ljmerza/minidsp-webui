from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    minidspd_url: str = "http://127.0.0.1:5380"
    webui_bind: str = "0.0.0.0"
    webui_port: int = 8480
    db_path: str = "/data/minidsp-webui.sqlite3"
    log_level: str = "info"


settings = Settings()
