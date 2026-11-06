-- Inbound events (one row per accepted webhook delivery) and the transactional outbox.

CREATE TABLE events (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source           text        NOT NULL,             -- 'github', 'slack', ...
    delivery_id      text        NOT NULL,             -- X-GitHub-Delivery
    event_type       text        NOT NULL,             -- X-GitHub-Event
    action           text,
    installation_id  bigint,                           -- GitHub App installation (tenant key)
    payload          jsonb       NOT NULL,
    status           text        NOT NULL DEFAULT 'received'
                     CHECK (status IN ('received', 'ignored', 'processed', 'failed')),
    received_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT events_source_delivery_uniq UNIQUE (source, delivery_id)
);

CREATE INDEX events_installation_idx ON events (installation_id, received_at DESC);

CREATE TABLE outbox (
    id            bigserial   PRIMARY KEY,
    topic         text        NOT NULL,                -- Redis stream name, e.g. 'jobs:review'
    payload       jsonb       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz,
    attempts      integer     NOT NULL DEFAULT 0,
    last_error    text
);

-- The relay only ever scans unpublished rows; keep that scan tiny.
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;

-- Wake the relay immediately on new rows (it also polls as a fallback).
CREATE FUNCTION outbox_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('outbox', NEW.topic);
    RETURN NEW;
END;
$$;

CREATE TRIGGER outbox_notify_trg
    AFTER INSERT ON outbox
    FOR EACH ROW EXECUTE FUNCTION outbox_notify();
