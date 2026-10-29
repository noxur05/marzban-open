import { TopUsersQueryKey } from "components/Statistics";
import { useQuery } from "react-query";
import { fetch } from "service/http";
import { AdminsUsageRange, usageRangeKey, usageRangeQuery } from "./useAdminsUsage";

export type TopUserEntry = {
  username: string;
  /** Owning admin's username; null for an orphaned user. */
  admin: string | null;
  /** Active device count in `by_devices`, bytes inside the range in `by_traffic`. */
  value: number;
};

export type TopUsersResponse = {
  start: string;
  end: string;
  by_devices: TopUserEntry[];
  by_traffic: TopUserEntry[];
};

export const topUsersQueryKey = (r: AdminsUsageRange) => [TopUsersQueryKey, ...usageRangeKey(r)];

/**
 * `GET /api/admins/usage/top-users`: the Statistics page's Users tab, two
 * Top-10 lists (most active devices, most traffic in range) shown one at a
 * time. Same range and scope rules as
 * useAdminsUsage — the server locks non-sudo callers to their own users and
 * ranks every user for an unfiltered sudo.
 */
export const useTopUsers = (range: AdminsUsageRange, enabled = true) =>
  useQuery<TopUsersResponse, Error>({
    queryKey: topUsersQueryKey(range),
    queryFn: () => fetch("/admins/usage/top-users", { query: usageRangeQuery(range) }),
    enabled: enabled && !!range.start && !!range.end,
    // The lists only move as hourly buckets land, and device changes made in
    // the panel invalidate TopUsersQueryKey explicitly, so a longer staleTime
    // than the chart's is safe and spares the raw node_user_usages SUM on
    // every tab focus.
    staleTime: 5 * 60_000,
    keepPreviousData: true,
    retry: 1,
  });
