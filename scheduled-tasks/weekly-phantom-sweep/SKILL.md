---
name: weekly-phantom-sweep
description: Weekly scan of ListenBrainz scrobbles for phantom Shazam matches, presenting suspects for review and deletion.
---

You are maintaining a vinyl scrobbler system for ListenBrainz user `$LISTENBRAINZ_USER`. The system uses Shazam (via SongRec) on a Raspberry Pi to fingerprint audio from a USB microphone and scrobble recognized tracks to ListenBrainz. Shazam sometimes produces false-positive "phantom" matches — tracks the user didn't actually play.

## Objective

Scan the past 7 days of ListenBrainz scrobbles, identify likely phantom matches, and open a GitHub issue with the results for the user to review.

## Credentials and connection details

- ListenBrainz user: `$LISTENBRAINZ_USER` (env var)
- ListenBrainz token: `$LISTENBRAINZ_TOKEN` (env var)
- GitHub repo: clodpated/vinyl-scrobbler (use `gh` CLI)
- Blocklist file: `blocklist.txt` in the repo root

## Step 1: Fetch recent scrobbles

Use the ListenBrainz API to fetch all scrobbles from the past 7 days. Calculate the unix timestamp for 7 days ago and use the `min_ts` parameter to paginate forward (ascending order). The API endpoint is:

```
GET https://api.listenbrainz.org/1/user/$LISTENBRAINZ_USER/listens?min_ts={timestamp}&count=100
```

Include the header `Authorization: Token $LISTENBRAINZ_TOKEN`. Paginate by advancing `min_ts` to the max timestamp from each page. Sleep 1 second between requests to avoid rate limiting.

## Step 2: Detect phantoms

Group scrobbles into sessions using a 20-minute gap threshold. For each session with 3+ tracks, identify suspects:

**Sandwiched phantoms** (highest confidence): An artist that appears exactly once in the session, where both the immediately preceding and following tracks are by the same (different) artist. Mark these with `[S]`.

**Window phantoms**: An artist that appears exactly once in the session, where within a ±5 track window there is a dominant artist with 3+ appearances. Mark these with `[W]`.

Before flagging, check the blocklist file to skip already-blocked entries. The blocklist is tab-separated (artist\ttrack), case-insensitive.

## Step 3: Open a GitHub issue

If suspects are found, open a GitHub issue on `clodpated/vinyl-scrobbler` using the `gh` CLI:

```bash
gh issue create --repo clodpated/vinyl-scrobbler --title "Weekly phantom sweep: <date range>" --body "<body>"
```

The issue body should contain:

1. A summary line: "Found X phantom candidates in Y scrobbles from MM/DD - MM/DD"
2. A table or list of all suspects, formatted as:
   ```
   | # | Date | Tag | Artist - Track | Surrounded by |
   ```
   Include the `listened_at` timestamp and `recording_msid` for each entry (needed for deletion later).
3. Categorize suspects into sections:
   - **Likely phantoms** — completely unrelated artists (spa music in a rock session, etc.)
   - **Possible sample matches** — could be Shazam matching a sampled song
   - **Credit variants** — same artist with different featuring credits (probably false positives)
4. Instructions at the bottom:
   ```
   ## How to respond
   Reply to this issue listing which entries to delete and blocklist.
   Example: "Delete and block 1, 3, 5-8. Keep 2, 4."
   Then run the phantom cleanup in Claude Code to process your selections.
   ```
5. Add the label `phantom-sweep` to the issue (create the label first if it doesn't exist).

If NO suspects are found, still create a GitHub issue to confirm the sweep ran:

```bash
gh issue create --repo clodpated/vinyl-scrobbler \
  --title "Weekly phantom sweep: <date range> — clean" \
  --body "Scanned X scrobbles from MM/DD - MM/DD. No phantom candidates detected." \
  --label "phantom-sweep"
```

## Error handling

**A GitHub issue must always be created, even on failure.** If any step fails (API timeout, authentication error, network issue, parsing error), create a GitHub issue reporting the failure:

```bash
gh issue create --repo clodpated/vinyl-scrobbler \
  --title "Weekly phantom sweep: FAILED" \
  --body "The scheduled phantom sweep failed.\n\nError: <describe what went wrong>\n\nStep that failed: <step number and description>" \
  --label "phantom-sweep"
```

The user relies on seeing a `phantom-sweep` issue each week to know the task ran. No issue = no visibility.

## Important notes

- The user listens to a wide range of music. Artists like Billy Woods, Open Mike Eagle, Nickelus F, Armand Hammer, Run The Jewels, Mogwai, of Montreal, Menomena, etc. are all legitimate.
- Some artists have variant credits (feat. credits, different label attributions). Flag these but note they're likely false positives.
- Da Lench Mob, Parliament, Johnny "Guitar" Watson etc. can appear as sample matches in hip-hop sessions.
- Do NOT delete any scrobbles or modify the blocklist. This task only detects and reports. The user will confirm deletions separately.
