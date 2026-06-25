"""Module 14 — Step 4 Setup Analysis service package.

Houses :class:`app.services.analysis.step4_analysis_engine.Step4AnalysisEngine`,
which runs after Module 13 (Step 3 Screening) and before Module 15 (Step 5
Proposals). The engine reads passing Step 3 candidates plus their current
features / prices, classifies each setup, computes stop / target / RR and the
Step 4 component scores, applies earnings / macro penalties, and appends one row
per analyzable candidate to ``step4_analysis``.
"""
