import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.citizen import Citizen

bearer_scheme = HTTPBearer()


def get_current_citizen(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Citizen:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    subject = decode_access_token(credentials.credentials)
    if subject is None:
        raise unauthorized

    try:
        citizen_id = uuid.UUID(subject)
    except ValueError:
        raise unauthorized

    citizen = db.get(Citizen, citizen_id)
    if citizen is None:
        raise unauthorized

    return citizen
