"""AbleEdge client errors. Messages never include secrets."""


class AbleEdgeError(Exception):
    """Base error for the AbleEdge client."""


class AbleEdgeUnreachableError(AbleEdgeError):
    """API / auth / transport failed — caller should apply fail-safe."""


class AbleEdgeAuthError(AbleEdgeUnreachableError):
    """Service-account token could not be obtained."""


class AbleEdgeConfigError(AbleEdgeError):
    """Binding or vendor configuration is unusable."""
