(function () {
  "use strict";

  window.PROTOTYPE_FIXTURES = {
    states: [
      "music-empty",
      "music-ready",
      "matching-running",
      "choices-complete-unselected",
      "choices-strong-selected",
      "review-sets",
      "review-focus",
      "build-ready",
      "build-running",
      "build-terminal"
    ],
    totals: {
      songs: 5031,
      spotify: 2819,
      soundcloud: 2354,
      both: 142,
      strong: 568,
      review: 2592,
      noSuitable: 1768,
      notPlayable: 12,
      noCandidate: 91,
      playable: 3160
    },
    reviewSets: [
      {
        id: "reasonable",
        title: "One reasonable option",
        count: 1140,
        description: "A close song match with one playable chart and a small metadata gap.",
        art: "cover-1"
      },
      {
        id: "versions",
        title: "Choose a version",
        count: 706,
        description: "Two or more good charts are available. Pick the version you prefer.",
        art: "cover-2"
      },
      {
        id: "recording",
        title: "Check the recording",
        count: 438,
        description: "A live, remix, acoustic, or recording-length difference may matter.",
        art: "cover-3"
      },
      {
        id: "quality",
        title: "Quality concerns",
        count: 308,
        description: "The match looks plausible, but instrument or chart-quality details need a look.",
        art: "cover-4"
      }
    ],
    musicSample: [
      { artist: "James Tillman", title: "And Then...", source: "Both", art: "cover-1" },
      { artist: "Little Dragon", title: "Twice", source: "Spotify", art: "cover-2" },
      { artist: "Men I Trust", title: "Show Me How", source: "SoundCloud", art: "cover-3" },
      { artist: "BADBADNOTGOOD", title: "Time Moves Slow", source: "Spotify", art: "cover-4" },
      { artist: "The Paper Satellites", title: "Everything Arrives at Once", source: "Both", art: "cover-5" }
    ],
    reviewQueue: [
      { artist: "Little Dragon", title: "Twice", state: "done", art: "cover-2" },
      { artist: "Men I Trust", title: "Show Me How", state: "done", art: "cover-3" },
      { artist: "James Tillman", title: "And Then...", state: "active", art: "cover-1" },
      { artist: "The Paper Satellites", title: "Everything Arrives at Once", state: "upcoming", art: "cover-5" },
      { artist: "BADBADNOTGOOD", title: "Time Moves Slow", state: "upcoming", art: "cover-4" },
      { artist: "Khruangbin", title: "Friday Morning", state: "upcoming", art: "cover-6" },
      { artist: "Hiatus Kaiyote", title: "Red Room", state: "upcoming", art: "cover-7" }
    ],
    currentReview: {
      sourceArtist: "James Tillman",
      sourceTitle: "And Then...",
      source: "Both",
      art: "cover-1",
      recommended: {
        artist: "James Tillman",
        title: "And Then",
        instruments: "Guitar, bass, drums",
        charter: "Chezy",
        concern: "The chart title omits the trailing punctuation, but the artist and recording length agree."
      },
      alternatives: [
        { key: "1", title: "And Then (Live)", meta: "Guitar, bass", charter: "HarmonicDust" },
        { key: "2", title: "And Then...", meta: "Guitar only", charter: "nightowl" },
        { key: "3", title: "And Then (Acoustic)", meta: "Guitar, vocals", charter: "Vega" }
      ]
    }
  };
}());
