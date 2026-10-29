import { AdminsUsageResponse } from "contexts/DashboardContext";
import dayjs from "dayjs";
import { useQuery } from "react-query";
import { fetch } from "service/http";
import { ASHGABAT_TZ } from "utils/dates";

export type AdminsUsageRange = {
  start: Date | null;
  end: Date | null;
  /** Sudo only; "" = every admin. Non-sudo callers are scoped server-side. */
  adminUsername?: string;
};

/** Cache-key tail for a Statistics-page range; every hook keyed on the
 * page's range builds its key as [prefix, ...usageRangeKey(r)] so they can
 * never drift apart. */
export const usageRangeKey = (r: AdminsUsageRange) => [
  r.start ? dayjs(r.start).format("YYYY-MM-DD") : null,
  r.end ? dayjs(r.end).format("YYYY-MM-DD") : null,
  r.adminUsername || "",
];

export const adminsUsageQueryKey = (r: AdminsUsageRange) => ["admins-usage", ...usageRangeKey(r)];

/**
 * Query string for a Statistics-page range: whole local days (Asia/Ashgabat,
 * interpreted server-side), plus the sudo admin filter. Shared by every
 * endpoint that feeds the page so they all read the same window.
 */
export const usageRangeQuery = (range: AdminsUsageRange): Record<string, string> => {
  const query: Record<string, string> = {};
  if (range.start) query.start = dayjs(range.start).format("YYYY-MM-DDT00:00:00");
  if (range.end) query.end = dayjs(range.end).format("YYYY-MM-DDT23:59:59");
  if (range.adminUsername) query.admin_username = range.adminUsername;
  return query;
};

/**
 * `GET /api/admins/usage` for a local-day range (interpreted server-side in
 * Asia/Ashgabat). Cached per range so flipping presets back and forth does
 * not refetch, with react-query's error state instead of a swallowed
 * rejection.
 */
export const useAdminsUsage = (range: AdminsUsageRange, enabled = true) =>
  useQuery<AdminsUsageResponse, Error>({
    queryKey: adminsUsageQueryKey(range),
    queryFn: () => fetch("/admins/usage", { query: usageRangeQuery(range) }),
    enabled: enabled && !!range.start && !!range.end,
    staleTime: 60_000,
    // A new range keeps the last one on screen while it loads: no blank flash.
    keepPreviousData: true,
    retry: 1,
  });

// The server clamps a range to this many days (ADMIN_USAGE_MAX_DAYS).
const MAX_RANGE_DAYS = 366;

/**
 * The span of equal length right before `range`, for the Overview tiles'
 * change against the previous period. Dates are the ones usageRangeQuery
 * sends, which the server reads as Asia/Ashgabat days. When the range ends
 * today the span is cut at the top of the Ashgabat hour the current data
 * was fetched in (`asOf`, its dataUpdatedAt) — the last hourly snapshot it
 * can hold — so today's partial day is compared with the same hours one span
 * earlier, even when the two queries were fetched at different times. The
 * hour is in the key, so a refetch of the current data in a new hour moves
 * the comparison with it. Keyed under "admins-usage", so whatever refreshes
 * the chart's query refreshes this one too.
 */
export const usePreviousUsage = (range: AdminsUsageRange, asOf: number, enabled = true) =>
  useQuery<AdminsUsageResponse, Error>({
    queryKey: [
      "admins-usage",
      "previous",
      ...usageRangeKey(range),
      dayjs(asOf).tz(ASHGABAT_TZ).format("YYYY-MM-DDTHH"),
    ],
    queryFn: () => {
      const day = (d: Date | null) => dayjs(dayjs(d).format("YYYY-MM-DD"));
      const end = day(range.end);
      const days = Math.min(end.diff(day(range.start), "day") + 1, MAX_RANGE_DAYS);
      const now = dayjs(asOf).tz(ASHGABAT_TZ);
      const cut =
        end.format("YYYY-MM-DD") >= now.format("YYYY-MM-DD")
          ? now.startOf("hour").subtract(days, "day").format("YYYY-MM-DDTHH:mm:ss")
          : end.subtract(days, "day").format("YYYY-MM-DDT23:59:59");
      const query: Record<string, string> = {
        start: end.subtract(2 * days - 1, "day").format("YYYY-MM-DDT00:00:00"),
        end: cut,
      };
      if (range.adminUsername) query.admin_username = range.adminUsername;
      return fetch("/admins/usage", { query });
    },
    enabled: enabled && asOf > 0 && !!range.start && !!range.end,
    staleTime: 60_000,
    keepPreviousData: true,
    retry: 1,
  });
