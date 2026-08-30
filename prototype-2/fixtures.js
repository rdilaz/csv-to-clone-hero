(function () {
  "use strict";

  window.PROTOTYPE_2_FIXTURES = {
    states: [
      "sources-empty",
      "music-ready",
      "matching-running",
      "charts-found",
      "download-ready",
      "download-running",
      "done"
    ],
    totals: {
      uniqueSongs: 5031,
      spotify: 2819,
      soundcloud: 2354,
      shared: 142,
      bestMatches: 568,
      bestAvailable: 2592,
      totalRecommendations: 3160,
      noSuitable: 1768,
      notPlayable: 12,
      noCandidate: 91
    },
    matching: {
      checked: 1842,
      elapsed: "8m 42s",
      current: {
        artist: "James Tillman",
        title: "And Then...",
        art: "cover-1"
      }
    },
    downloadReady: {
      alreadyAvailable: 422,
      destination: "Clone Hero / Songs",
      spaceCheck: "Enough available"
    },
    downloadRunning: {
      ready: 842,
      total: 3160,
      downloaded: 731,
      alreadyAvailable: 111,
      couldntAdd: 4,
      remaining: 2314,
      elapsed: "47m",
      current: {
        artist: "Little Dragon",
        title: "Twice",
        art: "cover-2"
      },
      inProgress: 2
    },
    done: {
      partialMissing: 4
    },
    sources: [
      {
        id: "spotify",
        label: "Spotify",
        description: "Liked songs and saved albums",
        countKey: "spotify",
        action: "Add Spotify music",
        updateAction: "Update music",
        art: "source-spotify",
        mark: "S"
      },
      {
        id: "soundcloud",
        label: "SoundCloud",
        description: "Likes and playlists",
        countKey: "soundcloud",
        action: "Import SoundCloud music",
        updateAction: "Replace import",
        art: "source-soundcloud",
        mark: "sc"
      },
      {
        id: "apple",
        label: "Apple Music / iTunes",
        description: "Your library, in one place",
        art: "source-apple",
        mark: "♪",
        comingSoon: true
      },
      {
        id: "youtube",
        label: "YouTube Music",
        description: "Likes and saved mixes",
        art: "source-youtube",
        mark: "▶",
        comingSoon: true
      },
      {
        id: "tidal",
        label: "Tidal",
        description: "Albums and favorites",
        art: "source-tidal",
        mark: "T",
        comingSoon: true
      },
      {
        id: "local",
        label: "Local audio",
        description: "A folder on this device",
        art: "source-local",
        mark: "↗",
        comingSoon: true
      }
    ],
    musicSample: [
      { artist: "James Tillman", title: "And Then...", source: "Both", art: "cover-1" },
      { artist: "Little Dragon", title: "Twice", source: "Spotify", art: "cover-2" },
      { artist: "Men I Trust", title: "Show Me How", source: "SoundCloud", art: "cover-3" },
      { artist: "BADBADNOTGOOD", title: "Time Moves Slow", source: "Spotify", art: "cover-4" },
      { artist: "The Paper Satellites", title: "Everything Arrives at Once", source: "Both", art: "cover-5" }
    ],
    confirmationRows: [
      {
        id: "and-then",
        artist: "James Tillman",
        title: "And Then...",
        source: "Both",
        tier: "Best match",
        version: "And Then (2018 Remaster)",
        art: "cover-1",
        selected: true
      },
      {
        id: "twice",
        artist: "Little Dragon",
        title: "Twice",
        source: "Spotify only",
        tier: "Best match",
        version: "Twice (Album Version)",
        art: "cover-2",
        selected: true
      },
      {
        id: "show-me-how",
        artist: "Men I Trust",
        title: "Show Me How",
        source: "SoundCloud only",
        tier: "Best available",
        version: "Show Me How (Live Session)",
        art: "cover-3",
        concern: "Version concern",
        selected: true
      },
      {
        id: "time-moves-slow",
        artist: "BADBADNOTGOOD",
        title: "Time Moves Slow",
        source: "Spotify only",
        tier: "Best match",
        version: "Time Moves Slow",
        art: "cover-4",
        selected: true
      },
      {
        id: "everything-arrives",
        artist: "The Paper Satellites",
        title: "Everything Arrives at Once",
        source: "Both",
        tier: "Best available",
        version: "Everything Arrives at Once (Remastered)",
        art: "cover-5",
        selected: true
      },
      {
        id: "long-way-home",
        artist: "The National Parks",
        title: "A Long, Long Way Back Home",
        source: "SoundCloud only",
        tier: "Best available",
        version: "A Long, Long Way Back Home (Live)",
        art: "cover-6",
        concern: "Version concern",
        selected: false
      },
      {
        id: "rising-tide",
        artist: "Khruangbin",
        title: "Rising Tide",
        source: "Spotify only",
        tier: "Best match",
        version: "Rising Tide",
        art: "cover-7",
        selected: true
      }
    ]
  };
}());
