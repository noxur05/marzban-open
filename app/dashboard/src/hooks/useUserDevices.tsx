import { DeviceStatsQueryKey, StatisticsQueryKey, TopUsersQueryKey } from "components/Statistics";
import { ClientAppsQueryKey } from "constants/queryKeys";
import { useDashboard } from "contexts/DashboardContext";
import { fetch } from "service/http";
import { useMutation, useQuery, useQueryClient } from "react-query";

export type Device = {
  id: number;
  hwid: string | null;
  /** Micky: stable device id (survives reinstalls), or its key hash on older builds. */
  device_id?: string | null;
  user_agent: string | null;
  client_name: string | null;
  client_version: string | null;
  platform: string | null;
  os_version: string | null;
  device_model: string | null;
  locale: string | null;
  last_ip: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  fetch_count: number;
  is_blocked: boolean;
};

export const userDevicesQueryKey = (username: string) => ["user-devices", username];

export const useUserDevices = (username: string | null | undefined) => {
  return useQuery<Device[], Error>({
    queryKey: userDevicesQueryKey(username || ""),
    queryFn: () => fetch(`/user/${username}/devices`),
    enabled: !!username,
    staleTime: 30_000,
  });
};

/**
 * One-way block of a specific device row. Backend enforces sudo-or-owner
 * and returns the updated device. On success we invalidate:
 *   - this user's devices list (the row drops out, server-side filter)
 *   - the users list (refreshes the per-row device_count icon)
 *   - DeviceStatsQueryKey (top-of-page Active Devices card)
 *   - StatisticsQueryKey (keeps the two cards in lockstep)
 */
export const useBlockDevice = (username: string | null | undefined) => {
  const qc = useQueryClient();
  return useMutation<Device, Error, number>({
    mutationFn: (deviceId) =>
      fetch(`/user/${username}/devices/${deviceId}/block`, { method: "POST" }),
    onSuccess: () => {
      if (username) qc.invalidateQueries(userDevicesQueryKey(username));
      qc.invalidateQueries(DeviceStatsQueryKey);
      qc.invalidateQueries(StatisticsQueryKey);
      qc.invalidateQueries(TopUsersQueryKey);
      qc.invalidateQueries(ClientAppsQueryKey);
      useDashboard.getState().refetchUsers();
    },
  });
};

/**
 * Delete a user's bound devices, freeing their slots (blocked rows are kept
 * server-side — a block is one-way). Refreshes the devices list, the users
 * table (device_count) and the two top-of-page cards.
 */
export const useResetUserDevices = (username: string | null | undefined) => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, void>({
    mutationFn: () => fetch(`/user/${username}/reset-devices`, { method: "POST" }),
    onSuccess: () => {
      if (username) qc.invalidateQueries(userDevicesQueryKey(username));
      // The two top-of-page cards poll every 5 s; the users table and the
      // Statistics page's "Most devices" list would otherwise keep showing
      // the old device_count.
      useDashboard.getState().refetchUsers();
      qc.invalidateQueries(TopUsersQueryKey);
      qc.invalidateQueries(ClientAppsQueryKey);
    },
  });
};
