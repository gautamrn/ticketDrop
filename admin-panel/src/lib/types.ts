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
  tier: string;
  active: boolean;
  reason: string | null;
  last_polled_at: string | null;
  starts_at: string | null;
};

export type PreferenceRow = {
  id: string;
  label: string;
  kind: string;
  active: boolean;
};

export type OperatorDrop = {
  drop_id: string;
  change: string;
  state: string | null;
  detected_at: string;
  error: string | null;
  title: string;
  content_url: string | null;
  ticket_link: string | null;
  venue: string | null;
  detail: Record<string, unknown> | null;
};

export type Health = {
  redis: boolean;
  postgres: boolean;
  streams: { name: string; length: number }[];
  lag: number | null;
  pending: number | null;
  consumers: number;
  counts: Record<string, number>;
};
