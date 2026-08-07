import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GeoFrame RAW Assist",
  description: "旅写真の選定とRAW現像を、人の判断と一緒に進める写真レビュー支援ツール。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ja">
      <body>{children}</body>
    </html>
  );
}
