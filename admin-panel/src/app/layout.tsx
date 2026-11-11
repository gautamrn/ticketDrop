import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "TicketDrop — RA onsale signals",
  description: "Audited Resident Advisor onsale signals for followed artists and venues.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
