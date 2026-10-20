"""Default Discovery categories (tab name -> Spotify search terms), used until
the user saves data/discovery_config.json.

No ``app`` imports here: update_discovery.py runs as a plain script.
"""

# Keep search terms generic rather than exact official playlist names: those
# surface Spotify's own editorial playlists, which get filtered out anyway
DEFAULT_CATEGORIES = {
    "Top Hits": "top hits 2026 community",
    "Workout": "workout gym motivation playlist",
    "Lo-Fi Beats": "lofi beats chill study",
    "Synthwave": "synthwave retro 80s",
    "Focus": "deep focus instrumental playlist",
}
