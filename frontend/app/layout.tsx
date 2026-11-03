import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FOMO · Smart Money Research",
  description: "Análisis verificable de wallets Solana, ROI realizado y confianza estadística.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="es"><body>{children}</body></html>;
}
