"""Operator runner entry points for local PyCharm / CLI usage.

Thin command-line wrappers around the frozen application modules:

* :mod:`tools.init_prod_db`       -> Module 03 schema manager (apply prod schema)
* :mod:`tools.run_prod_pipeline`  -> Module 20 ``PipelineOrchestrator`` (prod role)
* :mod:`tools.run_debug_pipeline` -> Module 22 ``DebugModeController`` (debug only)

These scripts own no pipeline, schema, or provider logic; every domain action
is delegated to the approved module. Unlike library/service/database/provider
modules (where ``print`` is forbidden), these operator scripts print
human-facing success/failure messages and set process exit codes — that is
their entire job.
"""
