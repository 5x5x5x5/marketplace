"""Runtime-mutable configuration.

Everything an operator can tune at runtime via the admin API lives here.
No persistence in v1; the object resets on app restart.
"""

from dataclasses import dataclass, field
from typing import Any

from .models import ServiceType


@dataclass
class MarginFloor:
    """Platform's minimum spread per matched pair.

    Effective floor for a given quote = max(absolute, pct * buyer_price).
    `ceiling_multiplier` caps how far the buyer-price bump can go before the
    quote is rejected: rejected if (bump-corrected price > base * ceiling).
    """

    absolute: float = 0.0
    pct: float = 0.0
    ceiling_multiplier: float = 3.0


@dataclass
class Pipelines:
    """Ordered adjuster names for each side, applied to the base price."""

    buyer: list[str] = field(default_factory=list[str])
    seller: list[str] = field(default_factory=list[str])


class Config:
    """Singleton-style runtime config. Admin endpoints mutate this in place.

    `service_types`     id → ServiceType (base prices)
    `pipelines`         service_type_id → Pipelines (buyer/seller adjuster lists)
    `margin_floor`      platform's minimum spread rule
    `matching_strategy` name from `marketplace.matching.STRATEGIES`
    `adjuster_params`   adjuster_name → params dict (read by the adjuster)
    """

    def __init__(self) -> None:
        self.service_types: dict[str, ServiceType] = {}
        self.pipelines: dict[str, Pipelines] = {}
        self.margin_floor: MarginFloor = MarginFloor()
        self.matching_strategy: str = "cheapest_payout"
        self.adjuster_params: dict[str, dict[str, Any]] = {}

    def get_pipelines(self, service_type_id: str) -> Pipelines:
        return self.pipelines.get(service_type_id, Pipelines())

    def to_dict(self) -> dict[str, Any]:
        """Serialize the config for GET /admin/config."""
        return {
            "service_types": {k: v.model_dump(mode="json") for k, v in self.service_types.items()},
            "pipelines": {
                k: {"buyer": v.buyer, "seller": v.seller} for k, v in self.pipelines.items()
            },
            "margin_floor": {
                "absolute": self.margin_floor.absolute,
                "pct": self.margin_floor.pct,
                "ceiling_multiplier": self.margin_floor.ceiling_multiplier,
            },
            "matching_strategy": self.matching_strategy,
            "adjuster_params": self.adjuster_params,
        }
