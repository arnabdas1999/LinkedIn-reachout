"""Shared errors for the CLI and browser adapter."""


class StopRun(RuntimeError):
    """Fatal run error unless a specific subclass defines narrower recovery."""


class StopRequested(StopRun):
    """Operator requested normal shutdown."""


class AccountBlocked(StopRun):
    """Login, verification or a platform limit requires operator attention."""


class ContactNeedsReview(StopRun):
    """Hold this contact without stopping unrelated contacts."""


class ContactNotReady(StopRun):
    """A read-only page operation may recover after a bounded retry."""


class RetryCycle(StopRun):
    """Discard this browser session and resume after a cooldown."""


class ProfileNeedsReview(ContactNeedsReview):
    """A selected profile's identity cannot be verified before starting an action."""


class InvitationFormMissing(StopRun):
    """Connect was clicked, but no recognized invitation composer appeared."""


class InvitationHeld(StopRun):
    """No Send was clicked, and the refreshed verified profile still offers Connect."""
