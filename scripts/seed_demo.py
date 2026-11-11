"""
scripts/seed_demo.py

Creates 3 demo milestones in ProofPay's database with sample proof texts
that produce pass, fail, and unclear verdicts when run against the mock
verifier.

Run:
    MOCK_AI=1 python scripts/seed_demo.py
"""

import sys
import os
import asyncio

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MOCK_AI", "1")
os.environ.setdefault("BASE_URL", "http://localhost:8000")

import uuid
import db
import verifier

MILESTONES = [
    {
        "id": "demo-pass",
        "title": "Homepage redesign — DEMO (pass)",
        "amount": 750.00,
        "currency": "USD",
        "criteria": (
            "Deliver Figma source file with all components\n"
            "Final HTML/CSS matches approved mockup pixel-perfectly\n"
            "All pages pass WCAG 2.1 AA accessibility check\n"
            "Lighthouse performance score >= 90 on mobile\n"
            "No placeholder copy or images remain"
        ),
        "template": "freelance",
        "proof_text": (
            "All deliverables are completed and submitted.\n\n"
            "Figma source file delivered with full component library. "
            "The HTML/CSS has been implemented and matches the approved mockup. "
            "WCAG 2.1 AA check passed — axe DevTools shows zero violations. "
            "Lighthouse mobile score: 94 (confirmed via screenshot). "
            "All copy is finalized; no placeholder text remains.\n\n"
            "Invoice #INV-2024-089 attached. Delivery confirmed."
        ),
    },
    {
        "id": "demo-fail",
        "title": "Homepage redesign — DEMO (fail)",
        "amount": 500.00,
        "currency": "USD",
        "criteria": (
            "Deliver Figma source file with all components\n"
            "Final HTML/CSS matches approved mockup\n"
            "Lighthouse performance score >= 90 on mobile\n"
            "No placeholder copy or images remain"
        ),
        "template": "freelance",
        "proof_text": (
            "Incomplete submission — the work is not done.\n\n"
            "The Figma file was never delivered. "
            "The HTML is broken on mobile and has not been fixed. "
            "Lighthouse score is 54, well below the required 90. "
            "Several placeholder images and 'Lorem ipsum' copy are still present.\n\n"
            "This is rejected and incomplete. The work failed to meet requirements."
        ),
    },
    {
        "id": "demo-unclear",
        "title": "Agent purchase request — DEMO (unclear)",
        "amount": 150.00,
        "currency": "USD",
        "criteria": (
            "Vendor must be on the approved vendor list\n"
            "Purchase amount must not exceed $300 per transaction\n"
            "Reason must clearly relate to the current project sprint\n"
            "No subscription services without prior written approval"
        ),
        "template": "agent_spend",
        "proof_text": (
            "I'd like to purchase something from a vendor for the project. "
            "The amount should be around $150, maybe less. "
            "I think it relates to what we're working on this sprint, more or less. "
            "Not entirely sure if it's on the approved list. "
            "Let me know if you need anything else from me."
        ),
    },
]


async def seed():
    db.init_db()
    print("Seeding demo milestones…\n")

    for m in MILESTONES:
        # Skip if already exists
        existing = db.get_milestone(m["id"])
        if existing:
            print(f"  [SKIP] {m['id']} already exists")
            continue

        # Create in DB
        db.create_milestone(
            id=m["id"],
            title=m["title"],
            amount=m["amount"],
            currency=m["currency"],
            criteria=m["criteria"],
            template=m["template"],
            paypal_order_id="DEMO_ORDER_ID",
            approve_url=f"http://localhost:8000/?funded={m['id']}&token=DEMO_ORDER_ID",
        )
        # Mark as HELD (simulate authorized payment)
        db.update_milestone(m["id"], status="HELD", authorization_id="DEMO_AUTH_ID")

        # Run verification
        print(f"  Verifying '{m['title']}'…")
        ai_result = await verifier.verify(
            criteria=m["criteria"],
            template=m["template"],
            proof_text=m["proof_text"],
            proof_link="",
            file_bytes=None,
            mime=None,
        )

        verdict = ai_result["verdict"]
        new_status = {"pass": "RELEASED", "fail": "VOIDED", "unclear": "NEEDS_REVIEW"}[verdict]
        db.update_milestone(m["id"], status=new_status, ai_result=ai_result)

        print(f"  ✓ {m['id']}: verdict={verdict}, status={new_status}\n")

    print("Done. Run the server and open http://localhost:8000 to see the demo milestones.")


if __name__ == "__main__":
    asyncio.run(seed())
