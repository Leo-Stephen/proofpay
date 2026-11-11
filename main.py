"""
main.py — FastAPI application for ProofPay.
Serves the REST API and static frontend.
"""

import os
import uuid
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Optional

from fastapi import (
    FastAPI,
    HTTPException,
    UploadFile,
    File,
    Form,
    Path,
    Request,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import db
import paypal
from paypal import PayPalError
import verifier

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
PAYPAL_ENABLED = bool(os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_SECRET"))

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("ProofPay started. PayPal=%s MOCK_AI=%s", PAYPAL_ENABLED, verifier.MOCK_AI)
    yield


app = FastAPI(title="ProofPay API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Error handler for PayPalError
# ---------------------------------------------------------------------------

@app.exception_handler(PayPalError)
async def paypal_error_handler(request: Request, exc: PayPalError):
    return JSONResponse(
        status_code=502,
        content={"detail": exc.message, "paypal_error": exc.raw},
    )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateMilestoneRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    amount: float = Field(..., gt=0, le=100_000)
    currency: str = Field(default="USD", max_length=3)
    criteria: str = Field(..., min_length=1, max_length=5000)
    template: str = Field(default="freelance", pattern="^(freelance|agent_spend)$")


class DecideRequest(BaseModel):
    action: str = Field(..., pattern="^(release|refund)$")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "paypal_configured": PAYPAL_ENABLED,
        "mock_ai": verifier.MOCK_AI,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
    }


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

@app.get("/api/milestones")
async def list_milestones():
    return db.list_milestones()


@app.post("/api/milestones", status_code=201)
async def create_milestone(body: CreateMilestoneRequest):
    milestone_id = str(uuid.uuid4())[:8]  # short, URL-friendly

    if not PAYPAL_ENABLED:
        # Return a stub so the UI can still demo the flow
        approve_url = f"{BASE_URL}/?funded={milestone_id}&token=DEMO_ORDER_ID"
        milestone = db.create_milestone(
            id=milestone_id,
            title=body.title,
            amount=body.amount,
            currency=body.currency,
            criteria=body.criteria,
            template=body.template,
            paypal_order_id="DEMO_ORDER_ID",
            approve_url=approve_url,
        )
        return {
            **milestone,
            "_warning": (
                "PayPal credentials not configured. Set PAYPAL_CLIENT_ID and "
                "PAYPAL_SECRET in .env to enable real payments. "
                "Running in demo mode — payment steps are simulated."
            ),
        }

    order = await paypal.create_order(
        amount=body.amount,
        currency=body.currency,
        milestone_id=milestone_id,
        base_url=BASE_URL,
    )

    milestone = db.create_milestone(
        id=milestone_id,
        title=body.title,
        amount=body.amount,
        currency=body.currency,
        criteria=body.criteria,
        template=body.template,
        paypal_order_id=order["order_id"],
        approve_url=order["approve_url"],
    )
    return milestone


@app.get("/api/milestones/{id}")
async def get_milestone(id: Annotated[str, Path(max_length=50)]):
    m = db.get_milestone(id)
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    return m


@app.post("/api/milestones/{id}/authorize")
async def authorize_milestone(
    id: Annotated[str, Path(max_length=50)],
    body: dict,
):
    m = db.get_milestone(id)
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    if m["status"] != "PENDING_FUNDING":
        raise HTTPException(
            status_code=409,
            detail=f"Milestone is already in status '{m['status']}'; cannot authorize again.",
        )

    order_id = body.get("token")
    if not order_id:
        raise HTTPException(status_code=422, detail="'token' (PayPal order ID) is required.")

    if not PAYPAL_ENABLED or order_id == "DEMO_ORDER_ID":
        # Demo mode: skip real PayPal call
        updated = db.update_milestone(id, status="HELD", authorization_id="DEMO_AUTH_ID")
        return {"status": "HELD", "authorization_id": "DEMO_AUTH_ID"}

    result = await paypal.authorize_order(order_id)
    updated = db.update_milestone(
        id,
        status="HELD",
        authorization_id=result["authorization_id"],
    )
    return {"status": "HELD", "authorization_id": result["authorization_id"]}


@app.post("/api/milestones/{id}/proof")
async def submit_proof(
    id: Annotated[str, Path(max_length=50)],
    text: Annotated[Optional[str], Form()] = None,
    link: Annotated[Optional[str], Form()] = None,
    file: Annotated[Optional[UploadFile], File()] = None,
):
    m = db.get_milestone(id)
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    if m["status"] not in ("HELD", "PENDING_FUNDING"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot submit proof for a milestone in status '{m['status']}'. "
                "Only HELD (or demo PENDING_FUNDING) milestones accept proof."
            ),
        )
    if not text and not link and not file:
        raise HTTPException(
            status_code=422,
            detail="At least one of text, link, or file must be provided as proof.",
        )

    # Read file bytes safely (limit 10 MB)
    file_bytes: Optional[bytes] = None
    mime: Optional[str] = None
    if file:
        MAX_BYTES = 10 * 1024 * 1024
        file_bytes = await file.read(MAX_BYTES + 1)
        if len(file_bytes) > MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="File too large. Maximum allowed size is 10 MB.",
            )
        mime = file.content_type or "application/octet-stream"

    # Run AI verification
    ai_result = await verifier.verify(
        criteria=m["criteria"],
        template=m["template"],
        proof_text=text or "",
        proof_link=link or "",
        file_bytes=file_bytes,
        mime=mime,
    )

    verdict = ai_result.get("verdict", "unclear")
    new_status = m["status"]  # default: no change
    paypal_action_error: Optional[str] = None

    auth_id = m.get("authorization_id")

    if verdict == "pass":
        if PAYPAL_ENABLED and auth_id and auth_id != "DEMO_AUTH_ID":
            try:
                await paypal.capture_authorization(auth_id)
                new_status = "RELEASED"
            except PayPalError as e:
                paypal_action_error = f"AI approved but PayPal capture failed: {e.message}"
                new_status = "NEEDS_REVIEW"
        else:
            new_status = "RELEASED"

    elif verdict == "fail":
        if PAYPAL_ENABLED and auth_id and auth_id != "DEMO_AUTH_ID":
            try:
                await paypal.void_authorization(auth_id)
                new_status = "VOIDED"
            except PayPalError as e:
                paypal_action_error = f"AI rejected but PayPal void failed: {e.message}"
                new_status = "NEEDS_REVIEW"
        else:
            new_status = "VOIDED"

    else:  # unclear
        new_status = "NEEDS_REVIEW"

    db.update_milestone(id, status=new_status, ai_result=ai_result)
    response = {
        **ai_result,
        "status": new_status,
    }
    if paypal_action_error:
        response["paypal_warning"] = paypal_action_error
    return response


@app.post("/api/milestones/{id}/decide")
async def decide_milestone(
    id: Annotated[str, Path(max_length=50)],
    body: DecideRequest,
):
    m = db.get_milestone(id)
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found.")
    if m["status"] != "NEEDS_REVIEW":
        raise HTTPException(
            status_code=409,
            detail=f"Manual decision is only valid for NEEDS_REVIEW milestones (current: '{m['status']}').",
        )

    auth_id = m.get("authorization_id")
    new_status: str
    paypal_action_error: Optional[str] = None

    if body.action == "release":
        if PAYPAL_ENABLED and auth_id and auth_id != "DEMO_AUTH_ID":
            try:
                await paypal.capture_authorization(auth_id)
                new_status = "RELEASED"
            except PayPalError as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"PayPal capture failed: {e.message}",
                )
        else:
            new_status = "RELEASED"
    else:  # refund
        if PAYPAL_ENABLED and auth_id and auth_id != "DEMO_AUTH_ID":
            try:
                await paypal.void_authorization(auth_id)
                new_status = "VOIDED"
            except PayPalError as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"PayPal void failed: {e.message}",
                )
        else:
            new_status = "VOIDED"

    db.update_milestone(id, status=new_status)
    return {"status": new_status}


# ---------------------------------------------------------------------------
# Static files (must be last — catch-all)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")
