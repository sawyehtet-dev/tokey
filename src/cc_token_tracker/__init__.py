"""cc_token_tracker package."""

# Single source of truth for the version: pyproject.toml reads this attribute
# (``[tool.setuptools.dynamic] version = {attr = ...}``), so a release bumps the
# number here and nowhere else. ``tokey --version`` reads it straight from the
# module, so it is correct regardless of when the package was last reinstalled.
__version__ = "0.7.6"

from cc_token_tracker.parser import TranscriptRecord, Usage, parse_line

__all__ = ["Usage", "TranscriptRecord", "__version__", "parse_line"]
