from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    # Fernet key for encrypting secrets at rest (node SSH material, the
    # Remnawave API token when it is stored per-deployment).
    encryption_key: str

    # The Remnawave panel this deployment manages its nodes through. One
    # panel owns every node, unlike 3x-ui where each VPS ran its own -- so
    # this is a single URL and a single token rather than per-node
    # credentials. The URL may or may not end in /api; the client normalises
    # it either way.
    remnawave_base_url: str = ""
    remnawave_token: str = ""
    # Set only when the panel sits behind the documented Caddy auth guard.
    remnawave_caddy_token: str = ""

    # Port the remnawave-node container listens on for config pushed by the
    # panel. Not a user-facing port: node bootstrap firewalls it to the
    # panel's address only.
    remnawave_node_port: int = 2222

    # How the nodes reach the panel. Usually the main server's public
    # address; needed because the node has to be told who to trust, and
    # because the firewall rule on the node is written against it.
    remnawave_panel_address: str = ""

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
