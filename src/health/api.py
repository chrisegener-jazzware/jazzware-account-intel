"""FastAPI router for the health-digest module (JAZ-180).

Exposes per-account scores, per-AM rolled-up scores, and per-AM markdown
digests. Sits on top of `health.pipeline.run_pipeline` so the same code path
used by the CLI/cron also powers the API.

Endpoints
---------
GET /health/accounts/scored
    Run the full pipeline (optionally limited) and return one JSON object per
    company with score, flag, components, narrative, and signals.
GET /health/am/{am_email}/scores
    Same shape as above, filtered to a single AM's portfolio.
GET /health/am/{am_email}/digest
    Markdown digest for a single AM (worst-first, banded by flag).
GET /health/am
    Map of {am_email: [company_id, ...]} for the current portfolio.
GET /health/score/{company_id}
    Single-customer score on demand.

Real HubSpot company + ticket sources are wired in. Integration health is
stubbed (JAZ-91). AM mapping is stubbed (JAZ-92). Both swap in via their
existing modules without touching this router.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from .digest import group_by_am, render_am_digest
from .pipeline import run_pipeline
from .scoring import HealthScore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


def _score_to_jsonable(s: HealthScore) -> dict[str, Any]:
    return asdict(s)


def _scores_or_503(
    *, customer_id: str | None = None, lookback_days: int, limit: int | None, use_claude: bool
) -> list[HealthScore]:
    try:
        return run_pipeline(
            customer_id=customer_id,
            lookback_days=lookback_days,
            use_claude=use_claude,
            limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline failed")
        raise HTTPException(503, f"pipeline failed: {e}") from e


@router.get("/accounts/scored")
def list_scored_accounts(
    lookback_days: int = Query(90, ge=1, le=365),
    limit: int | None = Query(None, ge=1, le=500),
    use_claude: bool = Query(False, description="Render real Claude narratives (slower, costs $)"),
) -> list[dict[str, Any]]:
    """Score every active customer and return the full portfolio ranked worst-first.

    Defaults to `use_claude=False` so the endpoint is cheap to hit from the UI;
    the cron pipeline runs with use_claude=True.
    """
    scores = _scores_or_503(lookback_days=lookback_days, limit=limit, use_claude=use_claude)
    scores.sort(key=lambda s: s.score)  # worst first
    return [_score_to_jsonable(s) for s in scores]


@router.get("/am")
def list_am_portfolios(
    lookback_days: int = Query(90, ge=1, le=365),
    limit: int | None = Query(None, ge=1, le=500),
) -> dict[str, list[dict[str, Any]]]:
    """Return {am_email: [{customer_id, customer_name, score, flag}, ...]} for routing."""
    scores = _scores_or_503(
        lookback_days=lookback_days, limit=limit, use_claude=False
    )
    grouped = group_by_am(scores)
    out: dict[str, list[dict[str, Any]]] = {}
    for am, lst in grouped.items():
        out[am] = [
            {
                "customer_id": s.customer_id,
                "customer_name": s.customer_name,
                "score": s.score,
                "flag": s.flag,
            }
            for s in lst
        ]
    return out


@router.get("/am/{am_email}/scores")
def am_scores(
    am_email: str,
    lookback_days: int = Query(90, ge=1, le=365),
    limit: int | None = Query(None, ge=1, le=500),
    use_claude: bool = Query(False),
) -> list[dict[str, Any]]:
    scores = _scores_or_503(
        lookback_days=lookback_days, limit=limit, use_claude=use_claude
    )
    mine = [s for s in scores if (s.account_manager or "unassigned@jazzware.com") == am_email]
    if not mine:
        # Empty portfolio is valid; return [] rather than 404 to keep clients simple.
        return []
    mine.sort(key=lambda s: s.score)
    return [_score_to_jsonable(s) for s in mine]


@router.get("/am/{am_email}/digest", response_class=Response)
def am_digest(
    am_email: str,
    lookback_days: int = Query(90, ge=1, le=365),
    limit: int | None = Query(None, ge=1, le=500),
    use_claude: bool = Query(True, description="Render real Claude narratives"),
    fmt: str = Query("md", pattern="^(md|json)$"),
) -> Response:
    """Return the rendered AM digest as markdown (default) or JSON envelope."""
    scores = _scores_or_503(
        lookback_days=lookback_days, limit=limit, use_claude=use_claude
    )
    grouped = group_by_am(scores)
    mine = grouped.get(am_email, [])
    md = render_am_digest(am_email, mine)
    if fmt == "json":
        return Response(
            content=_dump_json(
                {
                    "am_email": am_email,
                    "count": len(mine),
                    "markdown": md,
                    "scores": [_score_to_jsonable(s) for s in mine],
                }
            ),
            media_type="application/json",
        )
    return Response(content=md, media_type="text/markdown")


@router.get("/score/{company_id}")
def single_score(
    company_id: str,
    lookback_days: int = Query(90, ge=1, le=365),
    use_claude: bool = Query(True),
) -> dict[str, Any]:
    scores = _scores_or_503(
        customer_id=company_id,
        lookback_days=lookback_days,
        limit=None,
        use_claude=use_claude,
    )
    if not scores:
        raise HTTPException(404, f"no score produced for {company_id}")
    return _score_to_jsonable(scores[0])


def _dump_json(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)
