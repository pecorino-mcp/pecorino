import sys

import jwt
from fastapi import Request
from fastapi.exceptions import HTTPException

from src.mcp_server.config import settings


def verify_oauth_token(request: Request) -> dict:
    if not settings.oauth_required:
        return {}

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        sys.stderr.write("[AUTH ERROR] Missing authorization token\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Bearer token required"'}
        )

    token = auth_header.split(" ")[1]

    try:
        payload = jwt.decode(token, settings.oauth_jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        sys.stderr.write("[AUTH ERROR] Token expired\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401,
            detail="Token expired",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Token expired"'}
        )
    except jwt.InvalidTokenError as e:
        sys.stderr.write(f"[AUTH ERROR] Invalid token: {str(e)}\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Invalid token"'}
        )

    if payload.get("iss") != settings.oauth_issuer:
        sys.stderr.write("[AUTH ERROR] Issuer mismatch\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=401,
            detail="Issuer mismatch",
            headers={"www-authenticate": 'Bearer error="invalid_token", error_description="Issuer mismatch"'}
        )

    resources = payload.get("resource") or payload.get("aud") or []
    if isinstance(resources, str):
        resources = [resources]

    if settings.oauth_resource not in resources:
        sys.stderr.write("[AUTH ERROR] Resource mismatch\n")
        sys.stderr.flush()
        raise HTTPException(
            status_code=403,
            detail="Invalid resource indicator (resource mismatch)",
            headers={"www-authenticate": 'Bearer error="invalid_target", error_description="Invalid resource indicator (resource mismatch)"'}
        )

    return payload
