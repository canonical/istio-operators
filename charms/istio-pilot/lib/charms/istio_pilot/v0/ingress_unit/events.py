from ops.framework import EventBase, EventSource, ObjectEvents


class RelationAvailableEvent(EventBase):
    """Event triggered when a relation is ready for requests."""


class RelationFailedEvent(EventBase):
    """Event triggered when something went wrong with a relation."""


class RelationReadyEvent(EventBase):
    """Event triggered when a remote relation has the expected data."""


class RelationEndpointEvents(ObjectEvents):
    available = EventSource(RelationAvailableEvent)
    ready = EventSource(RelationReadyEvent)
    failed = EventSource(RelationFailedEvent)
