from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Role, User

security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    sub: str
    email: str
    name: str
    role: Role


@lru_cache
def jwk_client() -> PyJWKClient:
    settings = get_settings()
    if not settings.cognito_region or not settings.cognito_user_pool_id:
        raise RuntimeError("Cognito is not configured")
    issuer = f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/{settings.cognito_user_pool_id}"
    return PyJWKClient(f"{issuer}/.well-known/jwks.json")


def decode_cognito_token(token: str) -> dict:
    settings = get_settings()
    if not settings.cognito_app_client_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Cognito authentication is not configured")
    try:
        signing_key = jwk_client().get_signing_key_from_jwt(token).key
        issuer = f"https://cognito-idp.{settings.cognito_region}.amazonaws.com/{settings.cognito_user_pool_id}"
        claims = jwt.decode(token, signing_key, algorithms=["RS256"], issuer=issuer, options={"verify_aud": False})
    except (jwt.PyJWTError, RuntimeError) as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token") from error
    token_use = claims.get("token_use")
    client_id = claims.get("aud") if token_use == "id" else claims.get("client_id")
    if token_use not in {"id", "access"} or client_id != settings.cognito_app_client_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is not valid for this application")
    return claims


def resolve_role(claims: dict) -> Role:
    groups = claims.get("cognito:groups", [])
    for group in groups:
        if group in Role._value2member_map_:
            return Role(group)
    # Cognito self-registration always starts as the least-privileged role.
    # Administrators promote users by assigning the supervisor or admin group.
    return Role.employee


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication is required")
    claims = decode_cognito_token(credentials.credentials)
    subject = claims.get("sub")
    email = claims.get("email")
    if not subject or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token is missing required identity claims")
    role = resolve_role(claims)
    user = db.scalar(select(User).where(User.cognito_sub == subject))
    name = claims.get("name") or email.split("@")[0]
    if user is None:
        user = User(cognito_sub=subject, email=email, name=name, role=role)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not user.active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is inactive")
    elif user.role != role or user.email != email or user.name != name:
        user.role, user.email, user.name = role, email, name
        db.commit()
        db.refresh(user)
    return user


def require_roles(*roles: Role):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your role is not permitted to perform this action")
        return user

    return dependency
