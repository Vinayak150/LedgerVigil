/** Group exact API decimal strings without converting money to binary floating point. */
export function formatAmount(amount: string, currency: string): string {
  if (!/^-?\d+(\.\d+)?$/.test(amount)) return "Unavailable";
  const negative = amount.startsWith("-");
  const [whole, fraction] = (negative ? amount.slice(1) : amount).split(".");
  const grouped =
    currency === "INR" && whole.length > 3
      ? whole.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",") +
        "," +
        whole.slice(-3)
      : whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "−" : ""}${grouped}${fraction === undefined ? "" : "." + fraction}`;
}
export function periodRange(start: string, exclusiveEnd: string): string {
  const date = new Date(exclusiveEnd + "T00:00:00Z");
  date.setUTCDate(date.getUTCDate() - 1);
  const fmt = (value: Date) =>
    new Intl.DateTimeFormat("en-GB", {
      day: "numeric",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    }).format(value);
  return `${fmt(new Date(start + "T00:00:00Z"))} – ${fmt(date)}`;
}
