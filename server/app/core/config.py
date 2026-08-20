from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    # Fernet key for encrypting node panel credentials at rest.
    encryption_key: str

    # Shared secret the Telegram bot presents to call the internal API.
    internal_api_key: str

    cryptobot_api_token: str = ""


settings = Settings()
