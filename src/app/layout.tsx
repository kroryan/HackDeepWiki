import type { Metadata } from "next";
import { Geist_Mono, Noto_Sans_JP, Noto_Serif_JP } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "next-themes";
import { LanguageProvider } from "@/contexts/LanguageContext";
import CyberBackground from "@/components/CyberBackground";

// next/font self-hosts these at build time instead of the previous manual
// <link> to Google Fonts, which only ever loaded them at runtime from
// fonts.googleapis.com -- a real cost for an app that's meant to run fully
// offline against a local Ollama install (see README). It also silences
// @next/next/no-page-custom-font, though that rule is really aimed at the
// Pages Router's per-page <Head>; this file is the App Router root layout,
// so it already applied to every page regardless.
const geistMono = Geist_Mono({
  subsets: ["latin"],
  weight: ["400"],
  variable: "--nf-geist-mono",
  display: "swap",
});

const notoSansJP = Noto_Sans_JP({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--nf-noto-sans-jp",
  display: "swap",
});

const notoSerifJP = Noto_Serif_JP({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--nf-noto-serif-jp",
  display: "swap",
});

export const metadata: Metadata = {
  title: "HackDeepWiki Open Source | Sheing Ng",
  description: "Created by Sheing Ng",
};

export default function RootLayout({
  children
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistMono.variable} ${notoSansJP.variable} ${notoSerifJP.variable}`}
    >
      <body className="antialiased relative min-h-screen">
        <ThemeProvider attribute="data-theme" defaultTheme="system" enableSystem>
          <LanguageProvider>
            <CyberBackground />
            {children}
          </LanguageProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
