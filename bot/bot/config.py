from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    server_api_url: str
    internal_api_key: str

    # Comma-separated Telegram user IDs allowed to use the admin menu.
    bot_admin_ids: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.bot_admin_ids.split(",") if x.strip()}


settings = Settings()
