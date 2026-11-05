import inspect
from collections.abc import Iterator

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from orders_app.main import create_app
from orders_app.settings import OrdersSettings


def _walk(dependant: Dependant) -> Iterator[Dependant]:
    for sub in dependant.dependencies:
        yield sub
        yield from _walk(sub)


def test_dependencies_do_not_run_in_threadpool(settings: OrdersSettings) -> None:
    # Синхронные зависимости FastAPI выполняет в пуле потоков - 40 потоков на процесс.
    # Под нагрузкой они становятся ещё одним потолком. Ни одна наша зависимость ничего не блокирует,
    # поэтому все должны быть async def (или async-генераторами).
    # FromDishka у DishkaRoute тоже превращается в async-зависимость.
    app = create_app(settings)
    routes = [
        (f"{sorted(context.original_route.methods)} {context.path_format}", context.original_route)
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
    ]
    found = {name for name, _ in routes}
    assert {"['GET'] /v1/orders", "['POST'] /v1/orders", "['GET'] /v1/orders/{order_id}"} <= found, found

    sync_dependencies = {
        f"{name} → {dep.call.__qualname__}"
        for name, route in routes
        for dep in _walk(route.dependant)
        if dep.call is not None and not (inspect.iscoroutinefunction(dep.call) or inspect.isasyncgenfunction(dep.call))
    }
    assert not sync_dependencies, sync_dependencies
