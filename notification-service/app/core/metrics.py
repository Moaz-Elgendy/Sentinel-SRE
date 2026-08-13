from prometheus_client import Counter, Histogram

notification_deliveries_total = Counter(
    "notification_deliveries_total",
    "Total number of notification deliveries",
    ["channel", "result"]
)

notification_delivery_duration_seconds = Histogram(
    "notification_delivery_duration_seconds",
    "Simulated delivery time",
    ["channel"]
)
