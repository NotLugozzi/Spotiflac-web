-- Migration 002: Add retry mechanism and incomplete album tracking
-- Date: 2026-02-13


-- Add incomplete album tracking to albums table
ALTER TABLE albums ADD COLUMN is_incomplete BOOLEAN DEFAULT FALSE;
ALTER TABLE albums ADD COLUMN missing_tracks TEXT DEFAULT '[]';  -- JSON array stored as TEXT

-- Add retry tracking to downloads table
ALTER TABLE downloads ADD COLUMN retry_count INTEGER DEFAULT 0;
ALTER TABLE downloads ADD COLUMN max_retries INTEGER DEFAULT 3;
ALTER TABLE downloads ADD COLUMN failed_tracks TEXT DEFAULT '[]';  -- JSON array stored as TEXT

-- Update existing records to have default values
UPDATE albums SET is_incomplete = FALSE WHERE is_incomplete IS NULL;
UPDATE albums SET missing_tracks = '[]' WHERE missing_tracks IS NULL;
UPDATE downloads SET retry_count = 0 WHERE retry_count IS NULL;
UPDATE downloads SET max_retries = 3 WHERE max_retries IS NULL;
UPDATE downloads SET failed_tracks = '[]' WHERE failed_tracks IS NULL;
