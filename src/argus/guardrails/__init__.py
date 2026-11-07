"""Guardrails against indirect prompt injection and data leaks, in layers:

1. Untrusted content is wrapped as data (``wrap_untrusted``), with wrapper-escape
   attempts neutralised.
2. ``injection.classify`` flags likely injections; flagged runs get a reduced tool set
   and a visible warning.
3. Capability limits: per-agent tool allowlists, tools bound to the run's repo, write
   actions behind approval.
4. ``output.scan`` redacts secrets and PII before anything is posted or returned.
5. ``egress.EgressGuard`` lets HTTP clients reach only allowlisted hosts.
"""

from dataclasses import dataclass


@dataclass
class GuardSwitches:
    """Per-process switches, on in production. The injection eval turns layers off one
    at a time (ablation) to show what each layer is worth."""

    classifier: bool = True
    output_scan: bool = True


SWITCHES = GuardSwitches()
