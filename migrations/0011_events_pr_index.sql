-- Supersede checks look for a newer accepted push to the same PR before its run exists.
CREATE INDEX events_pull_request_idx ON events (
    installation_id,
    (payload -> 'repository' ->> 'id'),
    ((payload -> 'pull_request' ->> 'number'))
) WHERE event_type = 'pull_request';
