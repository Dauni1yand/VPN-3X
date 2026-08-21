from pydantic import BaseModel


class CloudflareConnectRequest(BaseModel):
    record_name: str
    # If omitted, the server auto-detects its own public IP -- see
    # app/services/cloudflare.py's detect_public_ip().
    server_ip: str | None = None
    admin_telegram_id: int
