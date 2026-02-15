import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.id3 import ID3

logger = logging.getLogger(__name__)

# Supported audio file extensions
SUPPORTED_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".ogg", ".opus", 
    ".wav", ".aiff", ".wma", ".alac"
}


class MetadataService:    
    def __init__(self):
        self.supported_extensions = SUPPORTED_EXTENSIONS
    
    def is_audio_file(self, path: str) -> bool:
        return Path(path).suffix.lower() in self.supported_extensions
    
    def read_metadata(self, file_path: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}")
            return None
        
        try:
            audio = MutagenFile(file_path, easy=True)
            
            if audio is None:
                logger.warning(f"Could not read audio file: {file_path}")
                return None
            
            metadata = self._extract_common_tags(audio, file_path)
            metadata.update(self._extract_technical_info(file_path))
            
            return metadata
        except Exception as e:
            logger.error(f"Error reading metadata from {file_path}: {e}")
            return None
    
    def _extract_common_tags(self, audio, file_path: str) -> Dict[str, Any]:
        def get_first(tag_list):
            if isinstance(tag_list, list):
                return tag_list[0] if tag_list else None
            return tag_list
        
        # Common tag mappings
        metadata = {
            "file_path": file_path,
            "file_name": os.path.basename(file_path),
            "title": get_first(audio.get("title")),
            "artist": get_first(audio.get("artist")),
            "album": get_first(audio.get("album")),
            "albumartist": get_first(audio.get("albumartist") or audio.get("album artist") or audio.get("band")),
            "track_number": None,
            "disc_number": None,
            "year": get_first(audio.get("date") or audio.get("year")),
            "genre": get_first(audio.get("genre")),
            "composer": get_first(audio.get("composer")),
            "isrc": get_first(audio.get("isrc")),
            "label": get_first(audio.get("label") or audio.get("organization")),
        }
        
        # Parse track number (can be "1/10" format)
        track_raw = get_first(audio.get("tracknumber") or audio.get("track"))
        if track_raw:
            try:
                if "/" in str(track_raw):
                    metadata["track_number"] = int(str(track_raw).split("/")[0])
                else:
                    metadata["track_number"] = int(track_raw)
            except (ValueError, TypeError):
                pass
        
        # Parse disc number
        disc_raw = get_first(audio.get("discnumber") or audio.get("disc"))
        if disc_raw:
            try:
                if "/" in str(disc_raw):
                    metadata["disc_number"] = int(str(disc_raw).split("/")[0])
                else:
                    metadata["disc_number"] = int(disc_raw)
            except (ValueError, TypeError):
                pass
        
        return metadata
    
    def _extract_technical_info(self, file_path: str) -> Dict[str, Any]:
        info = {
            "duration_ms": None,
            "bitrate": None,
            "sample_rate": None,
            "channels": None,
            "bits_per_sample": None,
            "file_format": Path(file_path).suffix.lower().lstrip("."),
            "file_size": os.path.getsize(file_path),
        }
        
        try:
            audio = MutagenFile(file_path)
            
            if audio is None:
                return info
            
            if hasattr(audio, "info"):
                audio_info = audio.info
                
                if hasattr(audio_info, "length"):
                    info["duration_ms"] = int(audio_info.length * 1000)
                
                if hasattr(audio_info, "bitrate"):
                    info["bitrate"] = audio_info.bitrate
                
                if hasattr(audio_info, "sample_rate"):
                    info["sample_rate"] = audio_info.sample_rate
                
                if hasattr(audio_info, "channels"):
                    info["channels"] = audio_info.channels
                
                if hasattr(audio_info, "bits_per_sample"):
                    info["bits_per_sample"] = audio_info.bits_per_sample
        except Exception as e:
            logger.debug(f"Error extracting technical info from {file_path}: {e}")
        
        return info
    
    def write_metadata(self, file_path: str, metadata: Dict[str, Any]) -> bool:

        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}")
            return False
        
        try:
            audio = MutagenFile(file_path, easy=True)
            
            if audio is None:
                logger.warning(f"Could not open audio file for writing: {file_path}")
                return False
            
            # Map metadata keys to tag names
            tag_mapping = {
                "title": "title",
                "artist": "artist",
                "album": "album",
                "albumartist": "albumartist",
                "year": "date",
                "genre": "genre",
                "composer": "composer",
            }
            
            for key, tag_name in tag_mapping.items():
                if key in metadata and metadata[key]:
                    audio[tag_name] = str(metadata[key])
            
            # Handle track number
            if "track_number" in metadata and metadata["track_number"]:
                if "total_tracks" in metadata and metadata["total_tracks"]:
                    audio["tracknumber"] = f"{metadata['track_number']}/{metadata['total_tracks']}"
                else:
                    audio["tracknumber"] = str(metadata["track_number"])
            
            # Handle disc number
            if "disc_number" in metadata and metadata["disc_number"]:
                audio["discnumber"] = str(metadata["disc_number"])

            audio.save()

            # Keep Vorbis BAND aligned with album artist where available
            if "albumartist" in metadata and metadata["albumartist"]:
                full_audio = MutagenFile(file_path)
                if full_audio is not None and getattr(full_audio, "tags", None) is not None:
                    format_name = type(full_audio).__name__.lower()
                    if "flac" in format_name or "ogg" in format_name or "opus" in format_name:
                        full_audio.tags["BAND"] = [str(metadata["albumartist"])]
                        full_audio.save()

            logger.info(f"Successfully wrote metadata to {file_path}")
            return True
        except Exception as e:
            logger.error(f"Error writing metadata to {file_path}: {e}")
            return False
    
    def scan_directory(
        self, 
        directory: str, 
        recursive: bool = True
    ) -> List[Dict[str, Any]]:
        results = []
        directory = Path(directory)
        
        if not directory.exists():
            logger.warning(f"Directory not found: {directory}")
            return results
        
        pattern = "**/*" if recursive else "*"
        
        for file_path in directory.glob(pattern):
            if file_path.is_file() and self.is_audio_file(str(file_path)):
                metadata = self.read_metadata(str(file_path))
                if metadata:
                    results.append(metadata)
        
        logger.info(f"Scanned {len(results)} audio files in {directory}")
        return results
    
    def get_album_art(self, file_path: str) -> Optional[bytes]:
        try:
            audio = MutagenFile(file_path)
            
            if audio is None:
                return None
            
            # FLAC
            if isinstance(audio, FLAC) and audio.pictures:
                return audio.pictures[0].data
            
            # MP3 with ID3
            if isinstance(audio, MP3) and audio.tags:
                for key in audio.tags:
                    if key.startswith("APIC"):
                        return audio.tags[key].data
            
            # M4A/MP4
            if isinstance(audio, MP4) and "covr" in audio.tags:
                covers = audio.tags["covr"]
                if covers:
                    return bytes(covers[0])
            
            # OGG
            if isinstance(audio, OggVorbis):
                metadata_block_picture = audio.get("metadata_block_picture")
                if metadata_block_picture:
                    import base64
                    from mutagen.flac import Picture
                    data = base64.b64decode(metadata_block_picture[0])
                    picture = Picture(data)
                    return picture.data
            
            return None
        except Exception as e:
            logger.debug(f"Error extracting album art from {file_path}: {e}")
            return None


_metadata_service: Optional[MetadataService] = None


def get_metadata_service() -> MetadataService:
    global _metadata_service
    if _metadata_service is None:
        _metadata_service = MetadataService()
    return _metadata_service
