-- Runs started by a request (Q&A) carry their input and requester.
ALTER TABLE runs
    ADD COLUMN input        jsonb,
    ADD COLUMN requested_by text;
