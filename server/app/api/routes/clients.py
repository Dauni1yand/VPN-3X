from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db.session import get_db
from app.schemas.clients import ClientCreate, ClientOut
from app.services.client_issuer import issue_client

router = APIRouter(prefix="/clients", tags=["clients"], dependencies=[Depends(require_internal_api_key)])


@router.post("", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate,
    db: AsyncSession = Depends(get_db),
    cf_ip_country: str | None = Header(default=None, alias="CF-IPCountry"),
) -> ClientOut:
    return await issue_client(
        db,
        payload.user_telegram_id,
        payload.duration_seconds,
        client_country=cf_ip_country,
        client_latencies=payload.client_latencies,
    )
