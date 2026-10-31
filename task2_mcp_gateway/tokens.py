"""Minimal HS256 JWT sign/verify using only the standard library.

Real deployments would verify RS256/ES256 tokens from an IdP (JWKS). HS256
with a shared secret keeps this assessment self-contained while still
proving the role claim cannot be forged without the key.
"""
import base64
import hashlib
import hmac
import json
import time


class TokenError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(claims: dict, secret: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def verify(token: str, secret: str, now: float | None = None) -> dict:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
        sig = _b64url_decode(sig_b64)
    except Exception:
        raise TokenError("malformed token") from None
    # Pin the algorithm: never trust the header to pick it (blocks alg=none).
    if header.get("alg") != "HS256":
        raise TokenError("unsupported alg")
    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise TokenError("bad signature")
    if not isinstance(claims, dict):
        raise TokenError("bad claims")
    exp = claims.get("exp")
    if exp is not None and (not isinstance(exp, (int, float)) or (now or time.time()) >= exp):
        raise TokenError("token expired")
    return claims


if __name__ == "__main__":
    # Demo helper: python -m task2_mcp_gateway.tokens <role> [secret]
    import sys

    role = sys.argv[1] if len(sys.argv) > 1 else "viewer"
    secret = sys.argv[2] if len(sys.argv) > 2 else "dev-secret-change-me"
    print(sign({"sub": f"demo-{role}", "role": role, "exp": int(time.time()) + 3600}, secret))
