import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "TicketDrop — Catch the signal",
    template: "%s — TicketDrop",
  },
  description:
    "Personal Resident Advisor discovery and audited onsale alerts for the artists and venues you follow.",
};

export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#050506",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
