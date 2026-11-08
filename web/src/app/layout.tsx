import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "LedgerVigil · Financial workspace",
  description:
    "A controlled workspace for financial investigation, verification and review.",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
