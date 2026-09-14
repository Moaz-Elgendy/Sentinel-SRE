"""
Seed the predefined government services catalog.

Run with: python -m app.seed
Idempotent — safe to run multiple times.
"""
from app.core.database import SessionLocal
from app.models.service import GovernmentService

SERVICES = [
    {
        "name": "National ID Renewal",
        "description": "Renew an expired or soon-to-expire national ID card.",
        "required_documents": ["Old national ID", "Recent photo"],
        "estimated_processing_days": 7,
    },
    {
        "name": "Passport Renewal",
        "description": "Renew an expired or soon-to-expire passport.",
        "required_documents": ["Old passport", "Recent photo", "National ID"],
        "estimated_processing_days": 14,
    },
    {
        "name": "Driver's License Renewal",
        "description": "Renew an expired driver's license.",
        "required_documents": ["Old license", "Medical certificate"],
        "estimated_processing_days": 5,
    },
    {
        "name": "Birth Certificate",
        "description": "Request an official copy of a birth certificate.",
        "required_documents": ["Parent national ID", "Hospital record"],
        "estimated_processing_days": 3,
    },
    {
        "name": "Marriage Certificate",
        "description": "Request an official copy of a marriage certificate.",
        "required_documents": ["National IDs of both spouses"],
        "estimated_processing_days": 3,
    },
    {
        "name": "Utility Complaint",
        "description": "File a complaint about a public utility service.",
        "required_documents": ["Utility bill"],
        "estimated_processing_days": 10,
    },
]


def seed():
    db = SessionLocal()
    try:
        for item in SERVICES:
            existing = db.query(GovernmentService).filter(GovernmentService.name == item["name"]).first()
            if existing is None:
                db.add(GovernmentService(**item))
        db.commit()
        print(f"Seeded {len(SERVICES)} government services (skipping any that already exist).")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
