/**
 * The shape the harness publishes to `ticket:drops:results`.
 *
 * Every value arrives as a string because Redis streams have no nested types,
 * so nothing here is pre-parsed into a number or boolean. Parsing happens at the
 * point of display, where a malformed field degrades one cell rather than
 * throwing away the whole event.
 */
export type ResultEvent = {
  drop_id?: string;
  event_id?: string;
  source?: string;
  change?: string;
  state?: string;
  outcome?: string;
  hype_score?: string;
  hype_components?: string;
  title?: string;
  venue?: string;
  link?: string;
  reservation_url?: string;
  mode?: string;
  forced_alert?: string;
  queue_it?: string;
  attempt_count?: string;
  healed_selector?: string;
  error?: string;
  at?: string;
};

export type FeedItem = ResultEvent & { entry_id: string };

export type TransitionRow = {
  from_state: string | null;
  to_state: string;
  latency_ms: number | null;
  detail: Record<string, unknown> | null;
  at: string;
};

export type WatchRow = {
  event_id: string;
  title: string;
  source: string;
  watch_source: string;
  tier: string;
  active: boolean;
  reason: string | null;
  last_polled_at: string | null;
  starts_at: string | null;
  pollable: boolean;
  resolved_to: string | null;
};

export type PreferenceRow = {
  id: string;
  label: string;
  kind: string;
  weight: number;
  active: boolean;
  has_embedding: boolean;
};

export type LinkRow = {
  event_id: string;
  canonical_event_id: string;
  match_confidence: number | null;
  match_method: string | null;
  confirmed: boolean;
  from_title: string;
  from_source: string;
  to_title: string;
  to_source: string;
  from_starts_at: string | null;
  to_starts_at: string | null;
};

export type Health = {
  redis: boolean;
  postgres: boolean;
  streams: { name: string; length: number }[];
  lag: number | null;
  pending: number | null;
  consumers: number;
  counts: Record<string, number>;
  soundScoringAvailable: boolean;
  activePreferences: number;
};
