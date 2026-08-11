-- The 13b orbit graph snapshot: precomputed daily by the worker so the
-- read path serves stored bytes (the zero-latency rule; the design's
-- own build note: no live force simulation on device OR on the read).
-- One row per user, replaced in place; payload is the full ratified
-- contract envelope (version, computed_at, caps, nodes, edges,
-- clusters, positions).

CREATE TABLE IF NOT EXISTS people_network_snapshots (
    user_id      TEXT PRIMARY KEY,
    computed_at  TIMESTAMPTZ NOT NULL,
    version      INT NOT NULL,
    payload      JSONB NOT NULL
);
