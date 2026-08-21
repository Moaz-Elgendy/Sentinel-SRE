from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_citizen
from app.models.citizen import Citizen
from app.schemas.citizen import CitizenOut, CitizenUpdate

router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("", response_model=CitizenOut)
def get_profile(current_citizen: Citizen = Depends(get_current_citizen)):
    return current_citizen


@router.put("", response_model=CitizenOut)
def update_profile(
    payload: CitizenUpdate,
    current_citizen: Citizen = Depends(get_current_citizen),
    db: Session = Depends(get_db),
):
    if payload.full_name is not None:
        current_citizen.full_name = payload.full_name
    if payload.phone is not None:
        current_citizen.phone = payload.phone

    db.commit()
    db.refresh(current_citizen)
    return current_citizen
