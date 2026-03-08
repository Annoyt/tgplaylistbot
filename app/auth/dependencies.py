"""FastAPI dependencies for auth."""

from fastapi import Request, HTTPException, status
from app.auth.service import decode_access_token


async def get_current_user(request: Request) -> dict:
    """Extract and verify JWT from cookie. Raises 401 if invalid."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"},
        )
    return payload
