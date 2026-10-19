-- Phase 9 (design section 12.2): per-channel "Not in use" and "Shakers (tactile)" flags, set in Settings.
-- unused: the channel is deliberately unused (its notes are hidden, a muted output folds into one row on Home).
-- tactile: an output that drives shakers / bass transducers (graphs and guided tasks treat it as tactile).

ALTER TABLE channel_meta ADD COLUMN unused INTEGER NOT NULL DEFAULT 0 CHECK (unused IN (0, 1));

ALTER TABLE channel_meta ADD COLUMN tactile INTEGER NOT NULL DEFAULT 0 CHECK (tactile IN (0, 1));
