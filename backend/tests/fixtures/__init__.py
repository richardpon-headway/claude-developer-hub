"""Shared test helpers — imperative seeders + builders.

Not a pytest plugin; plain Python functions. Tests import what they
need explicitly (``from tests.fixtures.config import write_minimal_config``)
rather than relying on autouse fixture wiring.
"""
