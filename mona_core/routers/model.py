from fastapi import Depends
from sqlalchemy.orm import Session

from mona_core.schemas import (
    MessageResponse
)
from mona_core.security import (
    get_db,
    user_router,
)

@user_router.get("/model", response_model=MessageResponse)
def delete_model(db: Session = Depends(get_db)):
    return {"state": "SUCCESS", "message": "ML don't don't available in demo"}
    