"""Module 23 — Config Recommender (learning layer) service package.

Houses :class:`app.services.learning.config_recommender.ConfigRecommenderService`,
which aggregates realized outcomes (prod ``signal_outcomes`` + simulation
``sim_signal_outcomes``) and proposes config changes for human review. Distinct
from ``app.services.outcomes`` (which tracks/realizes outcomes) and
``app.services.config`` (which stores/versions configs) — this package only
*reads* outcomes and *proposes* changes; it never activates a config. See
``M23_CONFIG_RECOMMENDER_SPEC.md``.
"""

from app.services.learning.config_recommender import ConfigRecommenderService

__all__ = ["ConfigRecommenderService"]
