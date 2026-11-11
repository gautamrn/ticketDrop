const AREA_NAMES: Record<number, string> = {
  8: "New York",
  13: "London",
  19: "Detroit",
  34: "Berlin",
};

export type RaArea = { id: number; name: string };

export function configuredRaAreas(): RaArea[] {
  const seen = new Set<number>();
  const areas: RaArea[] = [];
  for (const value of (process.env.RA_AREAS ?? "").split(",")) {
    const id = Number(value.trim());
    if (!Number.isInteger(id) || id <= 0 || seen.has(id)) continue;
    seen.add(id);
    areas.push({ id, name: AREA_NAMES[id] ?? `RA area ${id}` });
  }
  return areas;
}

export function configuredRaArea(id: number): RaArea | undefined {
  return configuredRaAreas().find((area) => area.id === id);
}
