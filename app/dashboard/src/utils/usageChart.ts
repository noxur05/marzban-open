import { AdminsUsageResponse } from "contexts/DashboardContext";
import { numberWithCommas } from "utils/formatByte";

export type Metric = "traffic" | "devices" | "users";

// "hourly" renders the raw points; "daily" rolls them up per calendar day.
export type Granularity = "hourly" | "daily";

type AdminPoints = AdminsUsageResponse["usages"][number]["points"];

// A single bucket's aggregated values for one admin.
type BucketValue = { traffic: number; devices: number; users: number };

/**
 * Categorical slots (validated with the dataviz palette checker against the
 * page surfaces: white and Chakra gray.800). The order is the CVD-safety
 * mechanism, so slots are assigned in order and never cycled: up to
 * MAX_SERIES admins each get a slot; past that, the top MAX_SERIES - 1 keep
 * theirs and the tail folds into "Others" in a neutral gray.
 */
export const SERIES_COLORS = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
};
export const OTHERS_COLOR = { light: "#A0AEC0", dark: "#718096" };

/** Most series the by-admin view draws: 8 admins, or the top 7 plus "Others". */
export const MAX_SERIES = SERIES_COLORS.light.length;

export type Axis = { max: number; tickAmount: number; format: (v: number) => string };

/**
 * A round step (1, 2, 2.5 or 5 × 10^k) cutting 0..top into about four
 * intervals; `integer` keeps it a whole number of at least 1.
 */
const niceStep = (top: number, integer: boolean) => {
  const raw = top / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const mult =
    [1, 2, 2.5, 5, 10].find((m) => {
      const s = m * pow;
      return s >= raw && (!integer || (s >= 1 && Number.isInteger(s)));
    }) ?? 10;
  const step = Math.max(integer ? 1 : 0, mult * pow);
  return { step, ticks: Math.max(1, Math.ceil(top / step - 1e-9)), mult, pow };
};

/**
 * A device/user y-axis with whole-number steps. ApexCharts' own scale with a
 * fixed tick count splits small totals into halves, which then round to
 * repeated labels ("0, 1, 1, 2, 2").
 */
export const countAxis = (maxCount: number): Axis => {
  const { step, ticks } = niceStep(Math.max(maxCount, 1), true);
  return {
    max: ticks * step,
    tickAmount: ticks,
    format: (v: number) => numberWithCommas(Math.round(v)) ?? "",
  };
};

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

/**
 * A traffic y-axis with round steps in ONE binary unit ("0.5 TB", "1 TB").
 * ApexCharts picks round *decimal* byte ticks, which formatBytes then shows
 * as "1 TB" twice; fixing max + tick count here keeps every label distinct.
 */
export const byteAxis = (
  maxBytes: number,
  e = maxBytes >= 1
    ? Math.min(BYTE_UNITS.length - 1, Math.floor(Math.log(maxBytes) / Math.log(1024)))
    : 3
): Axis => {
  const unit = 1024 ** e;
  // No traffic at all: a plain 0–1 GB frame rather than a sliver of a unit.
  const top = maxBytes >= 1 ? maxBytes / unit : 1;
  // Bytes don't split: whole steps when the unit is B.
  const { step, ticks, mult, pow } = niceStep(top, e === 0);
  // Just under a unit boundary the round top would read "1500 GB": say TB.
  if (ticks * step >= 1000 && e < BYTE_UNITS.length - 1) return byteAxis(maxBytes, e + 1);
  const decimals =
    e === 0 ? 0 : Math.max(0, -Math.round(Math.log10(pow))) + (mult === 2.5 ? 1 : 0);
  return {
    max: ticks * step * unit,
    tickAmount: ticks,
    format: (v: number) => (v ? `${+(v / unit).toFixed(decimals)} ${BYTE_UNITS[e]}` : "0"),
  };
};

/**
 * Map a point's timestamp to its bucket key:
 *   hourly -> the full "YYYY-MM-DDTHH:00:00" timestamp (raw point)
 *   daily  -> the "YYYY-MM-DD" date slice
 */
const bucketKey = (t: string, granularity: Granularity): string =>
  granularity === "hourly" ? t : t.slice(0, 10);

/** Sorted union of every bucket key seen across all admins. */
const collectBuckets = (
  data: AdminsUsageResponse | null,
  granularity: Granularity
): string[] => {
  if (!data) return [];
  const set = new Set<string>();
  for (const u of data.usages) {
    for (const p of u.points) set.add(bucketKey(p.t, granularity));
  }
  return Array.from(set).sort();
};

/** Python's round(): halves go to the even neighbour (0.5 -> 0, 1.5 -> 2). */
const roundHalfEven = (x: number) => {
  const r = Math.round(x);
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
};

/**
 * Roll one admin's hourly points up to the requested granularity:
 *   traffic = SUM of hourly traffic
 *   devices = ROUND(AVG of hourly device gauge)
 *   users   = ROUND(AVG of hourly user gauge)
 * — the server's own daily rollup (Python round), so every total on the page
 * is an exact sum of whole per-admin numbers. For hourly granularity each
 * bucket holds exactly one point, so the average collapses back to the raw
 * gauge.
 */
const aggregateAdmin = (
  points: AdminPoints,
  granularity: Granularity
): Record<string, BucketValue> => {
  const acc: Record<
    string,
    { traffic: number; devicesSum: number; usersSum: number; count: number }
  > = {};
  for (const p of points) {
    const key = bucketKey(p.t, granularity);
    const bucket = (acc[key] ||= {
      traffic: 0,
      devicesSum: 0,
      usersSum: 0,
      count: 0,
    });
    bucket.traffic += p.traffic;
    bucket.devicesSum += p.devices;
    bucket.usersSum += p.users;
    bucket.count += 1;
  }
  const out: Record<string, BucketValue> = {};
  for (const key in acc) {
    const b = acc[key];
    out[key] = {
      traffic: b.traffic,
      devices: b.count ? roundHalfEven(b.devicesSum / b.count) : 0,
      users: b.count ? roundHalfEven(b.usersSum / b.count) : 0,
    };
  }
  return out;
};

export type ChartSeries = {
  name: string;
  values: number[];
  /** Admins folded into this series; 1 for a real admin. */
  members: number;
  isOthers: boolean;
};

export type AdminRow = {
  admin_id: number;
  username: string;
  total_traffic: number;
  avgUsers: number;
  avgDevices: number;
};

export type UsageChart = {
  buckets: string[];
  /** Sum over every admin, per bucket, for the active metric. */
  totals: number[];
  /** Top admins by the active metric, then "Others" when there are too many. */
  series: ChartSeries[];
  /** Every admin, ordered like `series` (largest first). */
  rows: AdminRow[];
  totalTraffic: number;
  avgUsers: number;
  avgDevices: number;
  peakTraffic: number;
  peakBucket: string;
};

/**
 * Everything the Statistics chart, tiles and Admins tab read, in one pass.
 * Admins are ranked by the active metric — range traffic for traffic,
 * the range average for the gauges — and the same order drives the series
 * and their colours.
 */
export const buildUsageChart = (
  data: AdminsUsageResponse | null,
  metric: Metric,
  granularity: Granularity,
  othersLabel: (count: number) => string
): UsageChart => {
  const usages = data?.usages ?? [];
  const buckets = collectBuckets(data, granularity);

  const admins = usages.map((u) => {
    const agg = aggregateAdmin(u.points, granularity);
    // A missing bucket is no traffic, but a missing gauge reading is a gap,
    // not zero devices: the collector writes admins one by one, so the newest
    // hour can lack some of them. Carry the last reading forward; before an
    // admin's first point (not created yet) it is 0.
    let prev = 0;
    const values = buckets.map((b) => {
      if (agg[b]) prev = agg[b][metric];
      return agg[b] ? agg[b][metric] : metric === "traffic" ? 0 : prev;
    });
    const trafficByBucket = buckets.map((b) => (agg[b] ? agg[b].traffic : 0));
    // Range averages over the points the server sent: every hour for spans
    // up to 2 days, one (already averaged) point per day beyond that.
    let dSum = 0;
    let uSum = 0;
    for (const p of u.points) {
      dSum += p.devices;
      uSum += p.users;
    }
    const cnt = u.points.length;
    const avgUsers = cnt ? uSum / cnt : 0;
    const avgDevices = cnt ? dSum / cnt : 0;
    const rank =
      metric === "traffic" ? u.total_traffic : metric === "devices" ? avgDevices : avgUsers;
    return { u, values, trafficByBucket, avgUsers, avgDevices, rank };
  });
  admins.sort((a, b) => b.rank - a.rank || a.u.username.localeCompare(b.u.username));

  const totals = buckets.map((_, i) => admins.reduce((s, a) => s + a.values[i], 0));

  const shown = admins.length > MAX_SERIES ? admins.slice(0, MAX_SERIES - 1) : admins;
  const rest = admins.slice(shown.length);
  const series: ChartSeries[] = shown.map((a) => ({
    name: a.u.username,
    values: a.values,
    members: 1,
    isOthers: false,
  }));
  if (rest.length) {
    series.push({
      name: othersLabel(rest.length),
      values: buckets.map((_, i) => rest.reduce((s, a) => s + a.values[i], 0)),
      members: rest.length,
      isOthers: true,
    });
  }

  const rows: AdminRow[] = admins.map((a) => ({
    admin_id: a.u.admin_id,
    username: a.u.username,
    total_traffic: a.u.total_traffic,
    avgUsers: Math.round(a.avgUsers),
    avgDevices: Math.round(a.avgDevices),
  }));

  // Peak = the busiest single bucket's COMBINED traffic across all admins,
  // not one admin's best bucket.
  let peakTraffic = 0;
  let peakBucket = "";
  buckets.forEach((b, i) => {
    const sum = admins.reduce((s, a) => s + a.trafficByBucket[i], 0);
    if (sum > peakTraffic) {
      peakTraffic = sum;
      peakBucket = b;
    }
  });

  // Range totals for the gauge tiles are the SUM of each admin's OWN average
  // over the range (the average concurrent total across the system) — NOT a
  // global mean pooled across admins (which would wrongly divide by admin
  // count). These equal the sum of the per-admin averages on the Admins tab.
  return {
    buckets,
    totals,
    series,
    rows,
    totalTraffic: admins.reduce((s, a) => s + a.u.total_traffic, 0),
    avgUsers: Math.round(admins.reduce((s, a) => s + a.avgUsers, 0)),
    avgDevices: Math.round(admins.reduce((s, a) => s + a.avgDevices, 0)),
    peakTraffic,
    peakBucket,
  };
};
