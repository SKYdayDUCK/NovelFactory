"""Validate a Sign in with ChatGPT plan access token for this registered client."""
from functools import lru_cache

import jwt

from .providers import ProfileError

ISSUER = "https://auth.openai.com"
AUDIENCE = "https://api.openai.com/v1"
SCOPES = {"chatgpt.tokens.use.direct", "resource.invoke"}


@lru_cache(maxsize=1)
def _issuer_keys():
    # The official issuer publishes this endpoint in OIDC discovery. Ignore
    # token-supplied jku, jwk and x5u; use the signed kid solely for key choice.
    return jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json", lifespan=300, timeout=3)


def verify(token, client_id):
    try:
        if len(token) > 32768 or token.count(".") != 2:
            raise ValueError
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ("RS256", "ES256") or not isinstance(header.get("kid"), str):
            raise ValueError
        trusted_key = _issuer_keys().get_signing_key_from_jwt(token)
        if header["alg"] != trusted_key.algorithm_name:
            raise ValueError
        claims = jwt.decode(token, trusted_key.key, algorithms=[trusted_key.algorithm_name],
            issuer=ISSUER, audience=AUDIENCE, options={
                "strict_aud": True, "require": ["iss", "aud", "exp", "nbf", "iat", "sub", "client_id", "scope"]})
        if (claims["client_id"] != client_id or not isinstance(claims["scope"], str)
                or not SCOPES.issubset(claims["scope"].split())):
            raise ValueError
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, OSError):
        raise ProfileError("MODEL_PLAN_AUTH_UNVERIFIED") from None
