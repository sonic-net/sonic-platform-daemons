"""Placeholder; requester tests must inject the client they exercise."""


class GnoiClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("GnoiClient must be mocked by the unit test")
