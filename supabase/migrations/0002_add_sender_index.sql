-- Speeds up app.analytics.is_first_contact's per-sender lookup (checked on
-- every inbound message to decide whether to send the first-contact
-- welcome — see app.formatter.WELCOME_MESSAGE / app.main.webhook_handler),
-- which otherwise scans message_events with no index on `sender`.
create index if not exists message_events_sender_idx on message_events (sender);
