-- Migration: Add progress tracking fields to downloads table
-- Date: 2026-01-31
-- Description: Adds current_track_name, current_track_number, and total_tracks columns

ALTER TABLE downloads ADD COLUMN current_track_name VARCHAR(500);
ALTER TABLE downloads ADD COLUMN current_track_number INTEGER;
ALTER TABLE downloads ADD COLUMN total_tracks INTEGER;
