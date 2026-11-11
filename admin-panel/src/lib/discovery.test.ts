import { describe, expect, it } from "vitest";

import { discoveryReasons, normalizeGenres, parseListedPrice } from "./discovery";

const profile = {
  enabled: true,
  areaId: 19,
  areaName: "Detroit",
  genres: ["techno"],
  maxPrice: 40,
  currency: "USD" as const,
};

describe("discovery matching", () => {
  it("normalizes and deduplicates exact genre labels", () => {
    expect(normalizeGenres([" Techno ", "techno", "HOUSE", "", 2])).toEqual([
      "techno",
      "house",
    ]);
  });

  it("uses the highest explicitly listed price", () => {
    expect(parseListedPrice("$25 - $40")).toEqual({ maximum: 40, currency: "USD" });
    expect(parseListedPrice("£15 / £22.50")).toEqual({ maximum: 22.5, currency: "GBP" });
  });

  it("does not guess unknown currencies or non-price text", () => {
    expect(parseListedPrice("25 advance / 40 door")).toBeNull();
    expect(parseListedPrice("TBA")).toBeNull();
  });

  it("matches exact genres within budget with explainable reasons", () => {
    expect(discoveryReasons({ genres: ["Techno", "House"], cost: "$25 - $40" }, profile)).toEqual([
      "techno match",
      "Detroit metro",
      "listed at or below $40",
    ]);
  });

  it("rejects over-budget, unknown-price, and fuzzy genre candidates", () => {
    expect(discoveryReasons({ genres: ["Techno"], cost: "$45" }, profile)).toBeNull();
    expect(discoveryReasons({ genres: ["Techno"], cost: "TBA" }, profile)).toBeNull();
    expect(discoveryReasons({ genres: ["Hard Techno"], cost: "$20" }, profile)).toBeNull();
  });
});
