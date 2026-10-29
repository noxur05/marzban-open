import { ClientAppsQueryKey } from "constants/queryKeys";
import { useQuery } from "react-query";
import { fetch } from "service/http";
import {
  AdminsUsageRange,
  usageRangeKey,
  usageRangeQuery,
} from "./useAdminsUsage";

/** A device count under a name; `name` is null when the User-Agent gave none. */
export type NamedCount = {
  name: string | null;
  devices: number;
};

export type ClientAppStats = NamedCount & {
  /** Distinct users with at least one such device. */
  users: number;
  /** Devices whose first fetch is inside the range. */
  new_devices: number;
  /** Splits `devices` by platform, descending. */
  platforms: NamedCount[];
};

export type AdminClientApps = {
  /** null for users with no owning admin. */
  username: string | null;
  devices: number;
  /** Splits `devices` by app, descending. */
  clients: NamedCount[];
};

export type ClientAppsResponse = {
  start: string;
  end: string;
  devices: number;
  /** Distinct users over every app (one user on two apps counts once). */
  users: number;
  new_devices: number;
  clients: ClientAppStats[];
  admins: AdminClientApps[];
};

export const clientAppsQueryKey = (r: AdminsUsageRange) => [
  ClientAppsQueryKey,
  ...usageRangeKey(r),
];

/**
 * `GET /api/admins/usage/clients`: which client apps the devices in use during
 * the range run, overall and per admin, for the Statistics page's Apps and
 * Admins tabs. Same range and scope rules as useTopUsers; invalidated next to
 * it wherever devices change.
 */
export const useClientApps = (range: AdminsUsageRange, enabled = true) =>
  useQuery<ClientAppsResponse, Error>({
    queryKey: clientAppsQueryKey(range),
    queryFn: () =>
      fetch("/admins/usage/clients", { query: usageRangeQuery(range) }),
    enabled: enabled && !!range.start && !!range.end,
    // Devices only change on a fetch or a panel action, and the panel actions
    // invalidate ClientAppsQueryKey, so this can be as stale as Top users.
    staleTime: 5 * 60_000,
    keepPreviousData: true,
    retry: 1,
  });
