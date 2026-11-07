"""Re-export of the in-memory fakes (they live in ``argus.testing`` for the evals)."""

from argus.testing.fakes import GITHUB_API, FakeGitHub

__all__ = ["GITHUB_API", "FakeGitHub"]
