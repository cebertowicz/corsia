"""Static Microsoft protocol identifiers used by the connector."""

# Microsoft issues this tenant id for personal Microsoft accounts in a common flow.
CONSUMER_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"
#: Graph scopes, nothing for OpenID: the identity comes from Graph ``/me``.
SCOPES = ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite", "Files.ReadWrite", "offline_access", "User.Read")
