from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db.session import get_db
from app.schemas.settings import SettingUpdate
from app.services.audit import log_admin_action
from app.services.settings_store import get_all_settings, get_setting, set_setting

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_internal_api_key)])


@router.get("")
async def list_settings(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    return await get_all_settings(db)


@router.get("/{key}")
async def read_setting(key: str, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    try:
        value = await get_setting(db, key)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"key": key, "value": value}


@router.put("/{key}")
async def update_setting(key: str, payload: SettingUpdate, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    try:
        await set_setting(db, key, payload.value)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    log_admin_action(
        db, admin_telegram_id=payload.admin_telegram_id, action="set_setting", target=key, details=payload.value
    )
    await db.commit()
    return {"key": key, "value": payload.value}
