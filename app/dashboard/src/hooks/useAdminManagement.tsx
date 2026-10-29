import {
  ClientAppsQueryKey,
  DeviceStatsQueryKey,
  StatisticsQueryKey,
  TopUsersQueryKey,
} from "constants/queryKeys";
import { fetch } from "service/http";
import { useMutation, useQuery, useQueryClient } from "react-query";
import { useDashboard } from "contexts/DashboardContext";
import { AdminListItem, ADMINS_QUERY_KEY } from "./useAdmins";
import { ANNOUNCEMENT_KEYS } from "./useAnnouncements";

/** `POST /api/admin`. Every flag is sent: the backend's defaults (announcing
 * on) are not what a new reseller gets. Sudo is CLI-only, so never true. */
export type AdminCreateBody = {
  username: string;
  password: string;
  is_sudo: false;
  happ_only: boolean;
  can_announce: boolean;
  can_enable_outline: boolean;
  /** null = the panel default domain. */
  subscription_url: string | null;
  telegram_id: number | null;
};

/** Sudo-only: create an admin. A 409 means the name is taken in any case,
 * an env sudoer's included. */
export const useCreateAdmin = () => {
  const qc = useQueryClient();
  return useMutation<AdminListItem, any, AdminCreateBody>({
    mutationFn: (body) => fetch("/admin", { method: "POST", body }),
    onSuccess: async (admin) => {
      // A GET already in flight may predate the create: dropped, so it
      // can't land on top of the row added below.
      await qc.cancelQueries(ADMINS_QUERY_KEY);
      // In the list at once, so the new row (and "Open") is there before
      // the refetch lands.
      if (qc.getQueryData(ADMINS_QUERY_KEY))
        qc.setQueryData<AdminListItem[]>(ADMINS_QUERY_KEY, (old = []) => [
          ...old.filter((a) => a.username !== admin.username),
          { ...admin, users_count: 0 },
        ]);
      qc.invalidateQueries(ADMINS_QUERY_KEY);
      qc.invalidateQueries(ANNOUNCEMENT_KEYS.overview);
      qc.invalidateQueries("admins-usage");
    },
    // Taken: the cached list missed an admin created elsewhere.
    onError: (err) => {
      if (err?.response?.status === 409) qc.invalidateQueries(ADMINS_QUERY_KEY);
    },
  });
};

/** Fields `PATCH /api/admin/{username}` accepts. Absent = leave unchanged.
 * The forms leave a blank field out rather than send "": the backend stores
 * "" as-is, and for `subscription_url` reads it as "clear" (every link of the
 * admin moves to the panel default domain). */
export type AdminPatch = Partial<{
  password: string;
  telegram_id: number;
  discord_webhook: string;
  subscription_url: string;
  can_announce: boolean;
  can_enable_outline: boolean;
  happ_only: boolean;
}>;

/** Update one or more admin fields via PATCH — only the fields present in
 * the body are written, so nothing a caller forgets to send can be reset. */
export const useUpdateAdmin = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, { username: string; body: AdminPatch }>({
    mutationFn: ({ username, body }) =>
      fetch(`/admin/${encodeURIComponent(username)}`, { method: "PATCH", body }),
    onSuccess: () => {
      qc.invalidateQueries(ADMINS_QUERY_KEY);
      // Only the overview carries `can_announce`.
      qc.invalidateQueries(ANNOUNCEMENT_KEYS.overview);
    },
  });
};

/**
 * Refresh everything an admin-wide user operation can move: the user table
 * itself, plus the Statistics queries that count users and devices. The
 * admin list carries `users_usage`, so it goes too.
 *
 * `refetchUsers` is read off the store rather than through a hook so this stays
 * callable from a mutation callback (`useDashboard.getState()` has no React
 * lifecycle attached to it).
 */
const invalidateAfterBulkUserChange = (qc: ReturnType<typeof useQueryClient>) => {
  useDashboard.getState().refetchUsers();
  qc.invalidateQueries(ADMINS_QUERY_KEY);
  qc.invalidateQueries(StatisticsQueryKey);
  qc.invalidateQueries(DeviceStatsQueryKey);
  qc.invalidateQueries(TopUsersQueryKey);
  qc.invalidateQueries(ClientAppsQueryKey);
};

/**
 * Sudo-only: disable every `active` **and** `on_hold` user of one admin, in a
 * single request. `POST /api/admin/{username}/users/disable`.
 *
 * The endpoint restarts the local Xray core and every connected node once it
 * has written, so this is a brief drop for EVERY user on the panel, not only
 * this admin's — the calling UI has to say so.
 */
export const useDisableAdminUsers = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, string>({
    mutationFn: (username) =>
      fetch(`/admin/${encodeURIComponent(username)}/users/disable`, { method: "POST" }),
    onSuccess: () => invalidateAfterBulkUserChange(qc),
  });
};

/**
 * Sudo-only: re-enable this admin's disabled users.
 * `POST /api/admin/{username}/users/activate`.
 *
 * NOT a clean undo of the disable above: it reactivates *every* disabled user
 * of the admin, including ones disabled individually for unrelated reasons.
 * Same core+node restart.
 */
export const useActivateAdminUsers = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, string>({
    mutationFn: (username) =>
      fetch(`/admin/${encodeURIComponent(username)}/users/activate`, { method: "POST" }),
    onSuccess: () => invalidateAfterBulkUserChange(qc),
  });
};

export type DeleteAdminUsersResponse = { detail: string; removed: number };

/**
 * Sudo-only, PERMANENT: delete every user owned by one admin.
 * `DELETE /api/admin/{username}/users`.
 *
 * Separate from deleting the admin itself (`useDeleteAdmin`) — two explicit
 * steps so neither can trigger the other. The backend refuses sudo-owned
 * admins outright.
 */
export const useDeleteAdminUsers = () => {
  const qc = useQueryClient();
  return useMutation<DeleteAdminUsersResponse, Error, string>({
    mutationFn: (username) =>
      fetch(`/admin/${encodeURIComponent(username)}/users`, { method: "DELETE" }),
    onSuccess: () => invalidateAfterBulkUserChange(qc),
  });
};

/**
 * Sudo-only, PERMANENT: delete one admin account. `DELETE /api/admin/{username}`.
 *
 * The backend refuses a sudo admin (403, "use marzban-cli") and an admin that
 * still owns users (409): those go first, through `useDeleteAdminUsers`. So
 * no user changes here; its announcements go with it (ON DELETE CASCADE).
 */
export const useDeleteAdmin = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, string>({
    mutationFn: (username) =>
      fetch(`/admin/${encodeURIComponent(username)}`, { method: "DELETE" }).catch((err) => {
        // Another sudo deleted it first (the list is cached 5 minutes): what
        // was asked for, so it ends like a success. Only that 404: a name
        // with "/" in it misses the route and gets a bare "Not Found".
        if (err?.response?.status === 404 && err.response._data?.detail === "Admin not found") return null;
        throw err;
      }),
    onSuccess: (_, username) => {
      // A users table filtered to this admin would stay on a name the admin
      // dropdown no longer lists, with no way to pick "All admins" back.
      const { filters, onFilterChange } = useDashboard.getState();
      if (filters.admin === username) onFilterChange({ admin: undefined, offset: 0 });
      qc.invalidateQueries(ADMINS_QUERY_KEY);
      qc.invalidateQueries("announcement");
      qc.invalidateQueries("admins-usage");
    },
    // A 409 means it gained users since the dialog counted them.
    onError: (_, username) => {
      qc.invalidateQueries(ADMINS_QUERY_KEY);
      qc.invalidateQueries(["admin-user-count", username]);
    },
  });
};

/**
 * How many users an admin owns, so the delete-admin dialog can refuse before
 * the backend does. Refetched every time `enabled` turns on (staleTime 0), so a
 * count cached before "Delete all users" is never trusted. `limit: 1`, not 0:
 * the backend treats a falsy limit as "no limit" and would return them all.
 */
export const useAdminUserCount = (username: string | null, enabled: boolean) =>
  useQuery<number>(
    ["admin-user-count", username],
    () =>
      fetch<{ total: number }>("/users", {
        query: { admin: username, limit: 1 },
      }).then((r) => r.total),
    { enabled: enabled && !!username, staleTime: 0, refetchOnWindowFocus: false, retry: false }
  );

/** One "add this much traffic to this local day" instruction. `date` is an
 * Asia/Ashgabat (UTC+5) day "YYYY-MM-DD"; `gb` is GiB (1024^3 bytes). */
export type UsageAdjustItem = { date: string; gb: number };

export type UsageAdjustResponse = {
  detail: string;
  admin_id: number;
  username: string;
  total_added_bytes: number;
  applied: { date: string; added_bytes: number }[];
};

/** Sudo-only: add traffic to an admin on chosen past days. Corrects usage
 * this panel could not meter (external-subscription traffic is metered on
 * the remote panel). `POST /api/admin/usage/adjust/{username}`. */
export const useAdjustAdminUsage = () => {
  const qc = useQueryClient();
  return useMutation<
    UsageAdjustResponse,
    Error,
    { username: string; adjustments: UsageAdjustItem[] }
  >({
    mutationFn: ({ username, adjustments }) =>
      fetch(`/admin/usage/adjust/${encodeURIComponent(username)}`, {
        method: "POST",
        body: { adjustments },
      }),
    onSuccess: () => {
      qc.invalidateQueries(ADMINS_QUERY_KEY);
      // The Statistics page caches ranges; an adjustment rewrites history.
      qc.invalidateQueries("admins-usage");
    },
  });
};
