from prometheus_client import Counter, Histogram

MESSAGES_RECEIVED = Counter(
    "slhnet_messages_received_total",
    "Total Telegram updates received by the gateway",
)

COMMANDS_PROCESSED = Counter(
    "slhnet_commands_processed_total",
    "Total Telegram commands processed",
    ["command"],
)

REQUEST_DURATION = Histogram(
    "slhnet_request_duration_seconds",
    "HTTP request duration in seconds",
)
