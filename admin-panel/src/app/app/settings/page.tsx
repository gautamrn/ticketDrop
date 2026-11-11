import { auth } from "@/auth";
import { UserSettings } from "@/components/UserSettings";
import { query } from "@/lib/db";
import { configuredRaAreas } from "@/lib/ra-areas";

export default async function SettingsPage() {
  const session = await auth();
  const userId = session!.user.id;
  const settings = await query<{
    ra_area_id: number | null;
    ra_area_name: string | null;
  }>("SELECT ra_area_id, ra_area_name FROM user_settings WHERE user_id = $1::uuid", [
    userId,
  ]);
  const preferences = await query<{
    id: string;
    label: string;
    kind: string;
    active: boolean;
  }>(
    `SELECT id::text, label, kind::text AS kind, active
       FROM user_preferences WHERE user_id = $1::uuid
      ORDER BY kind, label`,
    [userId],
  );

  return (
    <UserSettings
      initial={settings[0] ?? null}
      email={session!.user.email ?? ""}
      preferences={preferences}
      areas={configuredRaAreas()}
    />
  );
}
