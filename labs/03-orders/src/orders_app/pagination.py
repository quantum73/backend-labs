from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Query, Request

if TYPE_CHECKING:
    from orders_app.settings import OrdersSettings


def get_page_limit_from_query(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> int:
    settings: OrdersSettings = request.state.settings
    return settings.page_default_limit if limit is None else limit


PageLimitFromQuery = Annotated[int, Depends(get_page_limit_from_query)]
