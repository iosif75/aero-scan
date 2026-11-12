"""Django admin and alert review, mounted at /admin by `aeroscan serve` (the `django` extra).

The monitor's database is opened read-only, as the API does. Django's own tables
(users, sessions) and the analysts' reviews live in a separate admin database, so the
poller's schema is never touched and a review never blocks a poll.
"""
