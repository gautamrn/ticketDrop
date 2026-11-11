export const DISCOVERY_CURRENCIES = ["USD", "GBP", "EUR"] as const;

export type DiscoveryCurrency = (typeof DISCOVERY_CURRENCIES)[number];

export type DiscoveryProfile = {
  enabled: boolean;
  areaId: number | null;
  areaName: string | null;
  genres: string[];
  maxPrice: number | null;
  currency: DiscoveryCurrency;
};

export type DiscoveryCandidate = {
  genres: string[];
  cost: string | null;
};

type ListedPrice = {
  maximum: number;
  currency: DiscoveryCurrency | null;
};

export function normalizeGenres(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  return [
    ...new Set(
      values
        .filter((value): value is string => typeof value === "string")
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean),
    ),
  ].slice(0, 8);
}

export function parseListedPrice(cost: string | null): ListedPrice | null {
  if (!cost) return null;
  if (/\bfree\b/i.test(cost)) return { maximum: 0, currency: null };

  const currency = /\bGBP\b|£/i.test(cost)
    ? "GBP"
    : /\bEUR\b|€/i.test(cost)
      ? "EUR"
      : /\bUSD\b|\$/i.test(cost)
        ? "USD"
        : null;
  if (!currency) return null;

  const amounts = [...cost.matchAll(/\d+(?:[.,]\d{1,2})?/g)]
    .map(([value]) => Number(value.replace(",", ".")))
    .filter(Number.isFinite);
  return amounts.length > 0 ? { maximum: Math.max(...amounts), currency } : null;
}

export function discoveryReasons(
  candidate: DiscoveryCandidate,
  profile: DiscoveryProfile,
): string[] | null {
  if (!profile.enabled || !profile.areaId || profile.genres.length === 0) return null;

  const eventGenres = new Set(normalizeGenres(candidate.genres));
  const matchedGenres = profile.genres.filter((genre) => eventGenres.has(genre));
  if (matchedGenres.length === 0) return null;

  const reasons = [
    `${matchedGenres.slice(0, 2).join(" + ")} match`,
    profile.areaName ? `${profile.areaName} metro` : `RA area ${profile.areaId}`,
  ];
  if (profile.maxPrice !== null) {
    const listed = parseListedPrice(candidate.cost);
    if (
      !listed ||
      (listed.currency !== null && listed.currency !== profile.currency) ||
      listed.maximum > profile.maxPrice
    ) {
      return null;
    }
    reasons.push(
      listed.maximum === 0
        ? "listed free"
        : `listed at or below ${formatMoney(profile.maxPrice, profile.currency)}`,
    );
  }
  return reasons;
}

export function formatMoney(amount: number, currency: DiscoveryCurrency): string {
  return new Intl.NumberFormat("en", {
    style: "currency",
    currency,
    maximumFractionDigits: amount % 1 === 0 ? 0 : 2,
  }).format(amount);
}
