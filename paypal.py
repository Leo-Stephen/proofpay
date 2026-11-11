"""
paypal.py — PayPal Orders v2 integration for ProofPay.
Covers: OAuth2 token (cached), create order (AUTHORIZE intent),
authorize, capture, void — with human-readable error extraction.
"""

import os
import time
import httpx
from typing import Optional

PAYPAL_BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
SECRET = os.getenv("PAYPAL_SECRET", "")

# In-memory token cache: (access_token, expires_at_unix)
_token_cache: tuple[str, float] = ("", 0.0)


class PayPalError(Exception):
    """Structured PayPal error with a human-readable message."""

    def __init__(self, message: str, status_code: int = 0, raw: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.raw = raw or {}


def _credentials_present() -> bool:
    return bool(CLIENT_ID and SECRET)


def _readable_error(resp: httpx.Response) -> str:
    """Extract the most helpful error string from a PayPal error response."""
    try:
        body = resp.json()
    except Exception:
        return f"PayPal returned HTTP {resp.status_code}: {resp.text[:300]}"

    # PayPal wraps errors differently depending on the endpoint
    name = body.get("name", "")
    message = body.get("message", "")
    details = body.get("details", [])
    detail_msgs = "; ".join(
        d.get("description", d.get("issue", "")) for d in details if isinstance(d, dict)
    )

    parts = []
    if name:
        parts.append(name)
    if message:
        parts.append(message)
    if detail_msgs:
        parts.append(f"({detail_msgs})")
    if not parts:
        parts.append(f"HTTP {resp.status_code}")

    return " — ".join(parts)


async def _get_token() -> str:
    """Return a cached or fresh OAuth2 access token."""
    global _token_cache
    access_token, expires_at = _token_cache
    # Refresh 60 seconds before expiry
    if access_token and time.time() < expires_at - 60:
        return access_token

    if not _credentials_present():
        raise PayPalError(
            "PayPal credentials missing. Set PAYPAL_CLIENT_ID and PAYPAL_SECRET in .env.",
            status_code=0,
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v1/oauth2/token",
            auth=(CLIENT_ID, SECRET),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=15,
        )

    if resp.status_code != 200:
        raise PayPalError(
            f"Failed to obtain PayPal access token: {_readable_error(resp)}",
            status_code=resp.status_code,
            raw=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {},
        )

    data = resp.json()
    access_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    _token_cache = (access_token, time.time() + expires_in)
    return access_token


async def _headers() -> dict:
    token = await _get_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def create_order(
    amount: float,
    currency: str,
    milestone_id: str,
    base_url: str,
) -> dict:
    """
    Create a PayPal order with AUTHORIZE intent.
    Returns {"order_id": str, "approve_url": str}.
    """
    headers = await _headers()
    payload = {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "reference_id": milestone_id,
                "amount": {
                    "currency_code": currency,
                    "value": f"{amount:.2f}",
                },
                "description": f"ProofPay milestone {milestone_id}",
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                    "brand_name": "ProofPay",
                    "locale": "en-US",
                    "landing_page": "LOGIN",
                    "user_action": "PAY_NOW",
                    "return_url": f"{base_url}/?funded={milestone_id}",
                    "cancel_url": f"{base_url}/?cancel={milestone_id}",
                }
            }
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders",
            json=payload,
            headers=headers,
            timeout=20,
        )

    if resp.status_code not in (200, 201):
        raise PayPalError(
            f"Could not create PayPal order: {_readable_error(resp)}",
            status_code=resp.status_code,
            raw=resp.json() if "application/json" in resp.headers.get("content-type", "") else {},
        )

    data = resp.json()
    order_id = data["id"]
    approve_url = next(
        (link["href"] for link in data.get("links", []) if link["rel"] == "payer-action"),
        None,
    )
    if not approve_url:
        raise PayPalError("PayPal order created but no approval URL returned.", raw=data)

    return {"order_id": order_id, "approve_url": approve_url}


async def authorize_order(order_id: str) -> dict:
    """
    Authorize a PayPal order that the buyer has approved.
    Returns {"authorization_id": str, "status": str}.
    """
    headers = await _headers()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/authorize",
            json={},
            headers=headers,
            timeout=20,
        )

    if resp.status_code not in (200, 201):
        raise PayPalError(
            f"Could not authorize PayPal order: {_readable_error(resp)}",
            status_code=resp.status_code,
            raw=resp.json() if "application/json" in resp.headers.get("content-type", "") else {},
        )

    data = resp.json()
    # Dig authorization id out of the response
    try:
        auth_id = (
            data["purchase_units"][0]["payments"]["authorizations"][0]["id"]
        )
        auth_status = (
            data["purchase_units"][0]["payments"]["authorizations"][0]["status"]
        )
    except (KeyError, IndexError) as exc:
        raise PayPalError(
            "PayPal authorization succeeded but response structure was unexpected.",
            raw=data,
        ) from exc

    return {"authorization_id": auth_id, "status": auth_status}


async def capture_authorization(authorization_id: str) -> dict:
    """
    Capture a previously-authorized payment (release funds to seller).
    Returns {"capture_id": str, "status": str}.
    """
    headers = await _headers()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v2/payments/authorizations/{authorization_id}/capture",
            json={"final_capture": True},
            headers=headers,
            timeout=20,
        )

    if resp.status_code not in (200, 201):
        raise PayPalError(
            f"Could not capture PayPal authorization: {_readable_error(resp)}",
            status_code=resp.status_code,
            raw=resp.json() if "application/json" in resp.headers.get("content-type", "") else {},
        )

    data = resp.json()
    return {"capture_id": data.get("id", ""), "status": data.get("status", "")}


async def void_authorization(authorization_id: str) -> dict:
    """
    Void a previously-authorized payment (return funds to buyer).
    Returns {"status": "VOIDED"}.
    """
    headers = await _headers()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v2/payments/authorizations/{authorization_id}/void",
            json={},
            headers=headers,
            timeout=20,
        )

    # PayPal returns 204 No Content on success
    if resp.status_code == 204:
        return {"status": "VOIDED"}

    if resp.status_code not in (200, 201):
        raise PayPalError(
            f"Could not void PayPal authorization: {_readable_error(resp)}",
            status_code=resp.status_code,
            raw=resp.json() if "application/json" in resp.headers.get("content-type", "") else {},
        )

    data = resp.json()
    return {"status": data.get("status", "VOIDED")}
