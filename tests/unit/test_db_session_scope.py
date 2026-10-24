"""Structural guard: every route depends on `get_db_session` with `scope="function"`.

Same idea as `test_rate_limit_coverage.py`. With FastAPI's default dependency scope the commit
after `yield` runs only once the response has been sent: a client can read back its own 201 before
the row is committed (a race the docker e2e suite hit), or get a 201 for a write whose commit then
fails. A second form of the dependency would also give one request two sessions.
"""

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from app.core.db import get_db_session
from app.main import app


def _session_dependants(dependant: Dependant, path: str) -> list[tuple[str, Dependant]]:
    found: list[tuple[str, Dependant]] = []
    for sub in dependant.dependencies:
        if sub.call is get_db_session:
            found.append((path, sub))
        found += _session_dependants(sub, path)
    return found


def _all_session_dependants() -> list[tuple[str, Dependant]]:
    found: list[tuple[str, Dependant]] = []
    for rc in iter_route_contexts(app.routes):
        route = rc.original_route
        if isinstance(route, APIRoute):
            found += _session_dependants(route.dependant, f"{sorted(route.methods)} {route.path}")
    return found


def test_routes_use_the_db_session() -> None:
    # Sanity: if the walk above ever stops finding the dependency, the next test passes vacuously.
    assert len(_all_session_dependants()) > 20


def test_every_db_session_dependency_is_function_scoped() -> None:
    wrong = sorted({path for path, dep in _all_session_dependants() if dep.scope != "function"})
    assert not wrong, (
        'Use Depends(get_db_session, scope="function") so the commit runs before the response '
        f"is sent; these routes don't: {wrong}"
    )
