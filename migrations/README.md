# Database Migrations

This directory contains database migration scripts for Spotiflac Web.

## Migration 001: Add Download Progress Fields

**Date:** 2026-01-31

**Description:** Adds progress tracking fields to the downloads table to show current track being downloaded and overall progress.

### New Fields
- `current_track_name` (VARCHAR(500)): Name of the track currently being downloaded
- `current_track_number` (INTEGER): Current track number (e.g., 1 in "1/17")
- `total_tracks` (INTEGER): Total number of tracks in the download

### Running the Migration

#### For Existing Databases

If you have an existing database, run the Python migration script:

```bash
python migrations/migrate_001.py
```

Or specify a custom database path:

```bash
python migrations/migrate_001.py /path/to/spotiflac.db
```

#### For New Installations

No action needed! The tables will be created automatically with the new fields when the app starts for the first time.

### Manual Migration (SQLite)

If you prefer to run the SQL migration manually:

```bash
sqlite3 ./data/spotiflac.db < migrations/001_add_download_progress_fields.sql
```

## Features Added

With this migration, downloads now show:
- Real-time track progress (e.g., "Track 3/17: Song Name - Artist")
- Percentage completion based on tracks downloaded
- Proper album name and artist from Spotify metadata
- "Download complete" message when finished
- Automatic library scan trigger after successful download
