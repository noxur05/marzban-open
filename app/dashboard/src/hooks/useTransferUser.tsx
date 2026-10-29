import {
  ClientAppsQueryKey,
  DeviceStatsQueryKey,
  StatisticsQueryKey,
  TopUsersQueryKey,
} from "constants/queryKeys";
import { useDashboard } from "contexts/DashboardContext";
import { useMutation, useQueryClient } from "react-query";
import { fetch } from "service/http";
import { ADMINS_QUERY_KEY } from "./useAdmins";

/**
 * Sudo-only: move one user to a different owning admin.
 * `PUT /api/user/{username}/set-owner?admin_username=<name>`.
 *
 * The backend also refreshes the user's Happ subscription link onto the new
 * owner's domain (see `crud.set_owner`), so the
 * response carries a changed `subscription_url` for Happ users — which is why
 * the users list is refetched rather than patched in place.
 *
 * `Admin.users_usage` is a lifetime counter and does NOT move with the user,
 * but both admins' totals are still re-read: the old owner stops accruing and
 * the new one starts, and the admin list is where that shows.
 */
export const useTransferUser = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, { username: string; adminUsername: string }>({
    mutationFn: ({ username, adminUsername }) =>
      fetch(`/user/${username}/set-owner`, {
        method: "PUT",
        query: { admin_username: adminUsername },
      }),
    onSuccess: () => {
      useDashboard.getState().refetchUsers();
      qc.invalidateQueries(ADMINS_QUERY_KEY);
      qc.invalidateQueries(StatisticsQueryKey);
      qc.invalidateQueries(DeviceStatsQueryKey);
      qc.invalidateQueries(TopUsersQueryKey);
      qc.invalidateQueries(ClientAppsQueryKey);
    },
  });
};
