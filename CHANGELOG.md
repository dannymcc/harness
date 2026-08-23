# Changelog

All notable changes to Harness are recorded here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-08-23

### Added

- An explicit **Dark** / **Light** button in the nav, and the dashboard now
  opens light for everyone rather than following the operating system (#2).
  The choice is kept in a `theme` cookie for a year, so it is remembered per
  browser. `data-theme` is rendered on the server, so a page arrives already
  in the right palette — no flash on load — and the toggle is a plain form
  post, so it works with JavaScript off. The `theme-color` meta follows the
  chosen theme too. The `/theme` post only redirects back to a referer on
  this host.

### Changed

- The README gained a short section on the palette and the toggle. The
  screenshots in the README and on the site still show the dark palette,
  which is now opt-in rather than what a new install looks like.

## [0.3.1] - 2026-08-23

### Fixed

- The footer version was hand-maintained and could name a build that wasn't
  running (#1). It is derived now: the number from `VERSION` in
  `harness/config.py`, the SHA from `HARNESS_GIT_SHA`, then the stamp the
  image build writes to `harness/_build_sha`, then `git HEAD` of the deployed
  checkout — each step guarded and timeout-bounded, since config is imported
  everywhere and this resolves once at import. When nothing can identify the
  build the footer says `(unknown build)` rather than implying a commit it
  doesn't know. The code for this shipped in the 0.2.0 image but was missed
  from that release's notes; it is recorded here.
- The README described the footer as `v0.1.0 (abc1234)` — the very thing #1
  was about, a version number kept by hand and left behind. The examples no
  longer carry a number.
- The README placed **Release now** on the project page only; since 0.3.0 it
  is on every card on the overview too.

## [0.3.0] - 2026-08-23

### Added

- **Release now** on every card on the overview, so a release can be forced
  from the page the desks are watched on rather than one level down. The
  override itself is not new — the request has always been honoured ahead of
  `release_min_changes` and `release_max_age_days` — but it was only reachable
  from the project page.
- A `release requested` pill on the overview card, so a press is acknowledged
  and the button cannot be fired twice by accident.

## [0.2.0] - 2026-08-23

### Added

- **Release now** on the project page cuts a release without waiting for the
  batch thresholds — including when the only changes on dev landed outside
  the harness, which previously did nothing at all.
- **Merge now** on an unreviewed pull request, for when you already know the
  answer and do not need Ruth's. The harness still merges it onto dev in its
  own clone and runs the suite before landing it, and still refuses drafts.
- Projects running `cut_release: auto` are marked as such on the overview and
  the project page, so hands-off repos are visible at a glance.

### Fixed

- The live console showed nothing for the whole of a run, under the words
  "Streaming — output appears as the agent works", which read as a stalled
  agent. A run's `log_path`, `turns` and `cost_usd` were only written when it
  finished, so the tail endpoint reported `live: false` and the poller gave
  up. Progress is recorded as it happens now.
- An auto release sat in `proposed` while it was being finalised, so the GUI
  offered a Merge & tag button for a release already on its way out; a click
  would have tried to merge a merged PR. It is marked `merging` first.
- An approved PR was merged on the strength of a test run from review time,
  which may have been several dev commits earlier. Every merge now re-runs
  the suite against the actual merge result.
- An auto release no longer counts toward the "needs your decision" tally on
  the overview, because it does not need one.

## [0.1.0] - 2026-08-22

First tagged release.
