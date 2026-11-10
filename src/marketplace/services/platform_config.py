"""Operator tuning: service types, pricing pipelines, the margin floor, fees,
the matching strategy and adjuster parameters. Composing and tuning needs no
code; adding an adjuster or strategy does (`pricing.py`, `matching.py`)."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import repo
from ..entities import Pipeline, ServiceType
from ..errors import NotFoundError, UnprocessableError
from ..matching import STRATEGIES
from ..models import FeesBody, MarginFloorBody, MatchingStrategyBody, PipelinesBody, ServiceTypeBody
from ..pricing import REGISTRY
from ..repo import audit


def get_config(session: Session) -> dict[str, Any]:
    pc = repo.get_platform_config(session)
    service_types = {
        st.id: {
            "base_buyer_price": str(st.base_buyer_price),
            "base_seller_payout": str(st.base_seller_payout),
        }
        for st in session.scalars(select(ServiceType)).all()
    }
    pipelines = {
        p.service_type_id: {"buyer": p.buyer, "seller": p.seller}
        for p in session.scalars(select(Pipeline)).all()
    }
    return {
        "service_types": service_types,
        "pipelines": pipelines,
        "margin_floor": {
            "absolute": str(pc.margin_absolute),
            "pct": str(pc.margin_pct),
            "ceiling_multiplier": str(pc.ceiling_multiplier),
        },
        "fees": {"pct": str(pc.fee_pct), "fixed": str(pc.fee_fixed)},
        "matching_strategy": pc.matching_strategy,
        "adjuster_params": pc.adjuster_params,
    }


def upsert_service_type(
    session: Session, admin_id: str, service_type_id: str, body: ServiceTypeBody
) -> ServiceType:
    st = session.get(ServiceType, service_type_id)
    if st is None:
        st = ServiceType(id=service_type_id)
        session.add(st)
    st.base_buyer_price = body.base_buyer_price
    st.base_seller_payout = body.base_seller_payout
    audit(session, admin_id, "upsert_service_type", service_type_id, body.model_dump(mode="json"))
    session.flush()
    return st


def upsert_pipelines(
    session: Session, admin_id: str, service_type_id: str, body: PipelinesBody
) -> dict[str, list[str]]:
    if session.get(ServiceType, service_type_id) is None:
        raise NotFoundError("unknown service_type_id")
    unknown = sorted({n for n in (*body.buyer, *body.seller) if n not in REGISTRY})
    if unknown:
        raise UnprocessableError(f"unknown adjuster(s): {unknown}")
    pipe = session.get(Pipeline, service_type_id)
    if pipe is None:
        pipe = Pipeline(service_type_id=service_type_id)
        session.add(pipe)
    pipe.buyer = body.buyer
    pipe.seller = body.seller
    audit(session, admin_id, "upsert_pipelines", service_type_id, body.model_dump(mode="json"))
    return {"buyer": pipe.buyer, "seller": pipe.seller}


def update_margin_floor(session: Session, admin_id: str, body: MarginFloorBody) -> dict[str, str]:
    pc = repo.get_platform_config(session)
    pc.margin_absolute = body.absolute
    pc.margin_pct = body.pct
    pc.ceiling_multiplier = body.ceiling_multiplier
    audit(session, admin_id, "update_margin_floor", "platform", body.model_dump(mode="json"))
    return {
        "absolute": str(pc.margin_absolute),
        "pct": str(pc.margin_pct),
        "ceiling_multiplier": str(pc.ceiling_multiplier),
    }


def update_fees(session: Session, admin_id: str, body: FeesBody) -> dict[str, str]:
    pc = repo.get_platform_config(session)
    pc.fee_pct = body.pct
    pc.fee_fixed = body.fixed
    audit(session, admin_id, "update_fees", "platform", body.model_dump(mode="json"))
    return {"pct": str(pc.fee_pct), "fixed": str(pc.fee_fixed)}


def update_matching_strategy(
    session: Session, admin_id: str, body: MatchingStrategyBody
) -> dict[str, str]:
    if body.strategy not in STRATEGIES:
        raise UnprocessableError(f"strategy must be one of {sorted(STRATEGIES.keys())}")
    pc = repo.get_platform_config(session)
    pc.matching_strategy = body.strategy
    audit(session, admin_id, "update_matching_strategy", "platform", {"strategy": body.strategy})
    return {"strategy": body.strategy}


def update_adjuster_params(
    session: Session, admin_id: str, adjuster_name: str, body: dict[str, Any]
) -> dict[str, Any]:
    if adjuster_name not in REGISTRY:
        raise NotFoundError(f"unknown adjuster {adjuster_name!r}")
    pc = repo.get_platform_config(session)
    pc.adjuster_params = {**pc.adjuster_params, adjuster_name: body}  # reassign so it persists
    audit(session, admin_id, "update_adjuster_params", adjuster_name, body)
    return body
