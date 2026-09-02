# Headache journal design (2026-09-01)

Bring headache tracking into anduin as the first real manual-logging surface.
The owner used to log headaches in Airtable (auto-filled with sleep / HRV /
RHR / TSS / weather by the sibling `auto-headache-tracker` repo) and stopped:
the Airtable UI was a chore, and a single "peak intensity" per day threw away
the shape of a day that was 2/10 all day with a 7/10 hour in the afternoon.
The health data those auto-fields came from already lives here; what was
missing was a low-friction way to log the headache itself.

Decisions are settled (conversation 2026-09-01). Analysis and correlations are
explicitly deferred -- there will not be enough data for months.

## The model: check-ins, not attacks

A **check-in** is "how is my head right now": a timestamp and a 0-10
intensity, with optional symptom detail. A day is a small series of check-ins,
and everything else -- peak, mean, headache-hours later -- is derived.

Rejected alternatives:

- **One row per day with a peak** (the Airtable model). Lossy in exactly the
  way that made the owner stop trusting it.
- **Discrete attacks with start/end** (Migraine Buddy). The owner's headaches
  are low-grade and near-constant with flare-ups; forcing them into episodes
  was the reason that app was abandoned.
- **Baseline + flare episodes.** The attack model with a baseline bolted on.

Check-ins pair naturally with reminders: each ntfy notification *is* a
check-in prompt, and answering "0" from the notification is one tap.

### What a check-in carries

- `intensity` 0-10 (required; the only field a "no headache" answer needs)
- `qualities`: any of pressure, throbbing, sharp, icepick, unilateral.
  "icepick" is kept separate from "sharp" because it names a specific
  sensation the owner recognises, narrower than the clinical term.
- `nausea` 0-3 (none / mild / moderate / severe), light and noise sensitivity,
  a free-text note
- `source`: `app` or `ntfy`, so compliance can be measured later
- `logged_at` (UTC) plus `tz_offset_minutes` and a stamped `local_date`

**Medication is out.** It is a separate feature the owner is thinking about
on its own terms; nothing here should be bent to accommodate it later beyond
the Log tab having room for a second card.

### Per-day context

One optional row per day (`journal.headache_days`):

- `fluorescent_exposure`: none / brief / hours. The most reliable recent
  trigger was working in an office, suspected to be the lighting. A "where
  did you work" field was considered and dropped -- with a work-from-home
  accommodation it no longer discriminates; the exposure does.
- `peak_intensity`: an optional override for a flare that came and went
  between check-ins, so it need not be faked as a backdated check-in.
- `coffee_cups`, `alcohol_drinks`: the two Airtable intake columns, as
  counts (0-20) so a later correlation has a number to work with. NULL is
  "not recorded", distinct from 0.
- `note`

### Unknown is not zero

A day with no check-ins is **unknown**, never headache-free. The metric is
never zero-filled (unlike steps), and `derived.headache_daily.peak` is NULL
for a day that has only a fluorescent row. The only way a day reads as
headache-free is an actual 0.

## Schema (migration 0026)

`journal` schema (user-entered observations), two tables, one derived view.
The daily view's `peak` is `greatest(checkin_peak, day_peak)`; both inputs are
exposed so analysis can distinguish "seen at a check-in" from "remembered at
the end of the day". Qualities are unioned per day via a lateral `unnest`
(aggregating `text[]` of differing lengths with `array_agg` errors).

Every range and enum is a CHECK constraint: the ntfy button posts straight to
the API with no browser in front of it.

## UI: the Log tab

A fourth bottom tab. Its landing page is the check-in form itself:

1. **Check-in card**: a 0-10 slider with a large readout (a row of eleven
   buttons was tried first and is cramped on a phone). The detail section is
   hidden while the slider sits at 0, so a "no headache" answer is one tap on
   Save. A collapsed "Different time" picker allows backfilling up to 30 days.
2. **Today card**: the fluorescent buttons (each its own one-tap submit), the
   day-peak override (also a slider), a coffee / alcohol intake form, a
   time-of-day strip of the check-ins, and the list with delete. No edit:
   delete-and-re-add is enough for v1.
3. **Recent days**: the last 14 days with peak (`*` when the override won),
   check-in count and fluorescent tag; each opens that day for correction.

The page's only JavaScript writes the browser's UTC offset into a hidden
field (so the civil date follows the phone, not the server) and mirrors the
sliders into their readouts, setting `data-zero` on the check-in form to hide
the detail at 0. Without the script the detail is simply always shown.

`/metrics` gains a "Journal" group with a Headache card (daily peak), and Home
gets a compact row that nudges when nothing has been logged today.

## Reminders (ntfy)

`anduin remind headache`, run by a systemd timer at four local times (09:00,
13:00, 17:00, 21:00 by default). Skipped when a check-in landed inside the
last 120 minutes. The notification has two actions -- inside iOS's cap of
three -- and no health data in its body:

- **No headache**: an `http` action; the phone's ntfy client POSTs
  `intensity=0&source=ntfy` to `/api/log/headache` over the tailnet and the
  notification clears. The app never opens.
- **Log**: a `view` action opening `/log`.

Public ntfy.sh for now: the topic name is the only access control, so it is a
secret (`NTFY_TOPIC` in the env file), and `NTFY_TOKEN` is wired for a
self-hosted server with ACLs later. `headache.app_url` must be the URL the
*phone* reaches anduin on, which means a reverse proxy or Tailscale Serve in
front of uvicorn's `127.0.0.1` bind.

Check-ins from the ntfy button carry no browser offset and take the server's
zone, so the host's timezone must be the owner's. While travelling, a browser
check-in and an ntfy check-in can land on different civil dates; documented,
not fixed.

The "adaptive" variant -- a follow-up an hour after any 5+ check-in to catch
how a flare resolves -- was considered and left for later; it is a small
addition to the same command if the fixed schedule misses flare shapes.

## Out of scope

Medication tracking, importing the old Airtable rows, weather / AQI / pollen,
correlation analysis. The daily view joins to `canonical.sleep`, `hrv_daily`
and `derived.pmc` on `local_date` with no schema change when the time comes.
