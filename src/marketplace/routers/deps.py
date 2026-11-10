"""Annotated FastAPI dependencies shared by the JSON routers."""

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from ..auth import Claims, current_buyer, current_participant, current_seller, require_admin
from ..db import get_session
from ..payments import get_provider
from ..payments.port import PaymentProvider

MAX_PAGE = 200

SessionDep = Annotated[Session, Depends(get_session)]
BuyerId = Annotated[str, Depends(current_buyer)]
SellerId = Annotated[str, Depends(current_seller)]
AdminId = Annotated[str, Depends(require_admin)]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
Offset = Annotated[int, Query(ge=0)]
ProviderDep = Annotated[PaymentProvider, Depends(get_provider)]
ParticipantClaims = Annotated[Claims, Depends(current_participant)]
