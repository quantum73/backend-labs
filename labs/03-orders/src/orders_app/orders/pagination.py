from uuid import UUID

from orders_app.core.pagination import filters_fingerprint


def orders_filters_fp(customer_id: UUID) -> str:
    return filters_fingerprint({"customer_id": str(customer_id)})
