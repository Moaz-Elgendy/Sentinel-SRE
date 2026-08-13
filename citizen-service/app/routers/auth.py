import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_citizen
from app.core.metrics import citizen_logins_total, citizen_registrations_total
from app.core.security import create_access_token, hash_password, verify_password
from app.models.citizen import Citizen
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.schemas.citizen import CitizenOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=CitizenOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    citizen = Citizen(
        full_name=payload.full_name,
        national_id=payload.national_id,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
    )
    db.add(citizen)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A citizen with this email or national ID already exists",
        )
    db.refresh(citizen)

    # Never log PII or secrets — just the fact that a registration happened.
    logger.info("citizen_registered", extra={"citizen_id": str(citizen.id)})
    citizen_registrations_total.inc()

    return citizen


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    citizen = db.query(Citizen).filter(Citizen.email == payload.email).first()

    if citizen is None or not verify_password(payload.password, citizen.password_hash):
        logger.info("login_failed", extra={"email": payload.email})
        citizen_logins_total.labels(result="failure").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    token = create_access_token(subject=str(citizen.id))
    logger.info("login_succeeded", extra={"citizen_id": str(citizen.id)})
    citizen_logins_total.labels(result="success").inc()
    return TokenResponse(access_token=token)


@router.get("/me", response_model=CitizenOut)
def me(current_citizen: Citizen = Depends(get_current_citizen)):
    return current_citizen
