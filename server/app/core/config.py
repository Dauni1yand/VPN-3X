from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    # Fernet key for encrypting node panel credentials at rest.
    encryption_key: str

    # Shared secret the Telegram bot presents to call the internal API.
    internal_api_key: str

    # Used only to push alert notifications straight to the admin(s)
    # (README: health-check alerting -> уведомление в тг). Same bot token as
    # the bot service; the server calls the Telegram HTTP API directly so
    # alerting doesn't depend on the bot process being up.
    telegram_bot_token: str = ""
    telegram_admin_ids: str = ""

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.telegram_admin_ids.split(",") if x.strip()]


settings = Settings()
