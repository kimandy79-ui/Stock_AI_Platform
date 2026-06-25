"""Module 15 — Step 5 Proposal Engine service package.

Houses :class:`app.services.proposal.step5_proposal_engine.Step5ProposalEngine`,
which runs after Module 14 (Step 4 Setup Analysis) and before Module 16. The
engine reads the Step 4 analyses for one ``signal_date`` / ``strategy_config_id``
(joining each analysis to its Step 3 screening score and its ticker's
sector / industry), computes a raw proposal score, assigns a raw ranking, applies
either hard-cap or soft-penalty diversification, and appends one row per
*analyzable* Step 4 analysis to ``step5_proposals`` in a single transaction.

Package-level import::

    from app.services.proposal import Step5ProposalEngine
"""

from app.services.proposal.step5_proposal_engine import Step5ProposalEngine

__all__ = ["Step5ProposalEngine"]
