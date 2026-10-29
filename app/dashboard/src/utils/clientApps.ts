import { ClientAppStats, NamedCount } from "hooks/useClientApps";
import { SERIES_COLORS } from "./usageChart";

/** The panel's own client app: it gets a tile and a colour of its own. */
export const MICKY = "Micky";
export const HAPP = "Happ";

/**
 * App identity colours: Happ and Micky by name, every other app one neutral.
 * A part-to-whole bar lets any two segments touch, and only three colours
 * stay apart when every pair is compared (validate_palette.js --pairs all:
 * blue / orange / #898781 pass in light and dark), so the long tail shares
 * the neutral and is told apart by its label.
 */
export const appColor = (name: string | null, dark: boolean) => {
  const p = SERIES_COLORS[dark ? "dark" : "light"];
  return name === HAPP ? p[0] : name === MICKY ? p[1] : OTHER_APPS_COLOR;
};
export const OTHER_APPS_COLOR = "#898781";

/** "84%", "4.3%", "<0.1%": whole percents from 10% up, one decimal below. */
export const percent = (part: number, whole: number) => {
  if (!whole || !part) return "0%";
  const v = (part / whole) * 100;
  if (v < 0.1) return "<0.1%";
  return v >= 10 ? `${Math.round(v)}%` : `${v.toFixed(1)}%`;
};

/** Device count of one app in a split, 0 when absent. */
export const countOf = (split: NamedCount[], name: string | null) =>
  split.find((c) => c.name === name)?.devices ?? 0;

/** Platform totals over every app, descending, unknown last. */
export const platformTotals = (clients: ClientAppStats[]): NamedCount[] => {
  const totals = new Map<string | null, number>();
  for (const c of clients)
    for (const p of c.platforms)
      totals.set(p.name, (totals.get(p.name) ?? 0) + p.devices);
  return [...totals.entries()]
    .map(([name, devices]) => ({ name, devices }))
    .sort(
      (a, b) =>
        Number(a.name === null) - Number(b.name === null) ||
        b.devices - a.devices
    );
};
