"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export function DemoReplayButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const replay = async () => {
    setBusy(true);
    const response = await fetch("/api/demo/replay", { method: "POST" });
    setBusy(false);
    if (!response.ok) {
      setError("Replay unavailable. Try again in a minute.");
      return;
    }
    setError("");
    router.push("/app/alerts");
    router.refresh();
  };

  return (
    <div className="mt-6 rounded-lg border border-warn/50 bg-ink-900 p-4">
      <p className="text-xs font-semibold uppercase tracking-wider text-warn">Demo data</p>
      <p className="mt-1 text-sm text-ink-300">
        Replay one clearly labeled synthetic onsale signal when no live event is changing.
      </p>
      <button
        type="button"
        disabled={busy}
        onClick={() => void replay()}
        className="mt-3 rounded-md border border-warn px-3 py-1.5 text-sm text-warn disabled:opacity-50"
      >
        {busy ? "Replaying…" : "Replay a signal"}
      </button>
      {error && <p role="alert" className="mt-2 text-xs text-danger">{error}</p>}
    </div>
  );
}
