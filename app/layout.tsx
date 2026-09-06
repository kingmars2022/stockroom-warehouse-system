import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Stockroom | Warehouse operations',
  description: 'Track inventory receipts, issues, stock levels, and movement history.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
