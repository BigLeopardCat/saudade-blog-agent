"""Construct requests for adapter unit tests without starting service dependencies."""

from fastapi import Request

from agent.main import app


def service_request(application=app):
    return Request(
        {
            "type": "http",
            "app": application,
            "method": "POST",
            "path": "/chat",
            "headers": [],
            "query_string": b"",
        }
    )
