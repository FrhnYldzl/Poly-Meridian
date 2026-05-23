import type { Metadata } from "next";
import { AgentStateProvider } from "@/hooks/use-shared-agent-state";
import "./globals.css";

export const metadata: Metadata = {
  title: "Poly Meridian — Operator",
  description: "Bloomberg-style operator dashboard for the Poly Meridian quant agent.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="dark">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="font-sans antialiased">
        <AgentStateProvider>{children}</AgentStateProvider>
      </body>
    </html>
  );
}
