"""Self-contained hub widgets.

Each widget under this package owns its routes, models, and storage and
touches CDH core in exactly one place — the router registration in
``app.main``. This keeps a widget addable/removable as a unit and sets
up the future yaml-driven enable/disable of widgets without untangling
it from the rest of the backend.
"""
