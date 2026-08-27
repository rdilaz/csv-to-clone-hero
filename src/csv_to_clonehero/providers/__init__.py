"""Music-library provider adapters.

Providers return the neutral :class:`LibrarySnapshot` contract. Chart sources
remain a separate concern in ``csv_to_clonehero.sources``.
"""

from csv_to_clonehero.providers.base import (
    LibraryProvider,
    LibrarySnapshot,
    ProviderPlaylist,
    ProviderTrack,
    SourceImportReport,
    merge_provider_tracks,
)
from csv_to_clonehero.providers.soundcloud import SoundCloudExportProvider
from csv_to_clonehero.providers.soundcloud_api import (
    SoundCloudAPIProvider,
    SoundCloudClient,
    SoundCloudError,
    SoundCloudOAuth,
    SoundCloudToken,
)
from csv_to_clonehero.providers.spotify import SpotifyLibraryProvider

__all__ = [
    "LibraryProvider",
    "LibrarySnapshot",
    "ProviderPlaylist",
    "ProviderTrack",
    "SourceImportReport",
    "SoundCloudExportProvider",
    "SoundCloudAPIProvider",
    "SoundCloudClient",
    "SoundCloudError",
    "SoundCloudOAuth",
    "SoundCloudToken",
    "SpotifyLibraryProvider",
    "merge_provider_tracks",
]
