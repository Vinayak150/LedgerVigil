"""Fiscal periods: list (read) and the audited, atomic period close (administer). Stage 2B Family 4; Stage 2A §8.1.

The close itself is implemented by the database function lv.close_period (migration 0009); this package only maps HTTP
to it and back. It never accepts a tenant or user from the request.
"""
