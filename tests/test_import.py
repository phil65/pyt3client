"""Smoke test: the package imports and reports its version."""

import pyt3client


def test_package_imports() -> None:
    """The package can be imported and exposes a version string."""
    assert isinstance(pyt3client.__version__, str)
