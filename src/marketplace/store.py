"""In-memory state. No persistence in v1."""

from uuid import UUID

from .models import (
    AvailabilityRecord,
    BuyerProfile,
    Job,
    Quote,
    SellerProfile,
    Transaction,
)


class Store:
    def __init__(self) -> None:
        self.quotes: dict[UUID, Quote] = {}
        self.jobs: dict[UUID, Job] = {}
        self.availability: dict[tuple[str, str], AvailabilityRecord] = {}
        self.sellers: dict[str, SellerProfile] = {}
        self.buyers: dict[str, BuyerProfile] = {}
        self.transactions: list[Transaction] = []

    # Availability
    def add_availability(self, seller_id: str, service_type_id: str) -> AvailabilityRecord:
        rec = AvailabilityRecord(seller_id=seller_id, service_type_id=service_type_id)
        self.availability[(seller_id, service_type_id)] = rec
        # Auto-register a default profile if the seller hasn't been seen.
        if seller_id not in self.sellers:
            self.sellers[seller_id] = SellerProfile(id=seller_id)
        return rec

    def remove_availability(self, seller_id: str, service_type_id: str) -> bool:
        return self.availability.pop((seller_id, service_type_id), None) is not None

    def available_for(self, service_type_id: str) -> list[AvailabilityRecord]:
        return [a for a in self.availability.values() if a.service_type_id == service_type_id]

    # Profiles — auto-create on first reference so ad-hoc IDs work.
    def get_or_create_seller(self, seller_id: str) -> SellerProfile:
        if seller_id not in self.sellers:
            self.sellers[seller_id] = SellerProfile(id=seller_id)
        return self.sellers[seller_id]

    def get_or_create_buyer(self, buyer_id: str) -> BuyerProfile:
        if buyer_id not in self.buyers:
            self.buyers[buyer_id] = BuyerProfile(id=buyer_id)
        return self.buyers[buyer_id]

    # Demand proxy: count of currently active (QUOTED or MATCHED) jobs for the service.
    def active_demand(self, service_type_id: str) -> int:
        from .models import JobStatus

        return sum(
            1
            for j in self.jobs.values()
            if j.service_type_id == service_type_id
            and j.status in (JobStatus.QUOTED, JobStatus.MATCHED)
        )

    # Transactions
    def record_transaction(self, tx: Transaction) -> Transaction:
        self.transactions.append(tx)
        return tx
