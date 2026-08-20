from pydantic import BaseModel


class SettingUpdate(BaseModel):
    value: str
    admin_telegram_id: int  # who changed it, for admin_audit_log
