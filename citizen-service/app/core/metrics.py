from prometheus_client import Counter

citizen_registrations_total = Counter(
    "citizen_registrations_total",
    "Total number of successful citizen registrations"
)

citizen_logins_total = Counter(
    "citizen_logins_total",
    "Total number of login attempts",
    ["result"]
)

service_requests_total = Counter(
    "service_requests_total",
    "Total number of service requests",
    ["status"]
)

notification_dispatches_total = Counter(
    "notification_dispatches_total",
    "Total number of notification dispatch attempts to notification-service",
    ["result"]
)
