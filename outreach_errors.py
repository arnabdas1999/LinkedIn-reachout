"""Shared errors for the CLI and browser adapter."""


class StopRun(RuntimeError):
    """Stop without guessing or retrying a possible send."""


class StopRequested(StopRun):
    """Operator requested normal shutdown."""


class ProfileNeedsReview(StopRun):
    """A selected profile's identity cannot be verified before starting an action."""
