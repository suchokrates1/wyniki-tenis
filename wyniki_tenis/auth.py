"""Authentication helpers for configuration endpoints."""

from __future__ import annotations

import os
from functools import wraps
from typing import Callable, Optional, Tuple

from flask import Response, current_app, request


Credentials = Tuple[str, str]


def get_config_auth_credentials() -> Credentials | None:
    username = os.environ.get("CONFIG_AUTH_USERNAME")
    password = os.environ.get("CONFIG_AUTH_PASSWORD")
    if username is None or password is None:
        return None
    return username, password


def unauthorized_response() -> Response:
    response = current_app.make_response(("Unauthorized", 401))
    response.headers["WWW-Authenticate"] = 'Basic realm="Overlay Config"'
    return response


def requires_config_auth(view_func: Callable):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        credentials = get_config_auth_credentials()
        if not credentials:
            return unauthorized_response()

        auth = request.authorization
        if auth and (auth.username, auth.password) == credentials:
            return view_func(*args, **kwargs)

        return unauthorized_response()

    return wrapper


__all__ = [
    "Credentials",
    "get_config_auth_credentials",
    "requires_config_auth",
    "unauthorized_response",
]
