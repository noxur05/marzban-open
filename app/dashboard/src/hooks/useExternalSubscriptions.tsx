import { useMutation, useQuery, useQueryClient } from "react-query";
import { fetch } from "service/http";

/** Last poll outcome, published to Redis by the Rust poller.
 *
 * Every field is optional: all-empty means the poller has not reported on this
 * row yet, which is also what a fresh Redis looks like. It is deliberately not
 * stored in MySQL — that is what keeps the poller read-only against the panel
 * database. */
export type ExternalSubscriptionStatus = {
  ok?: boolean | null;
  fetched_at?: string | null;
  http_status?: number | null;
  error?: string | null;
  host_counts?: Record<string, number>;
  countries?: string[];
  /** The REMOTE user's own quota and expiry, verbatim. When that user expires
   * or caps out, every eligible local user loses these hosts at once. */
  remote_userinfo?: string | null;
};

export type ExternalSubscription = {
  id: number;
  name: string;
  url: string;
  hwid?: string | null;
  enabled: boolean;
  /** Admins whose users receive these hosts. EMPTY = nobody, never everybody:
   * this is a billing boundary, so an unassigned subscription fails closed. */
  admin_ids: number[];
  created_at: string;
  status: ExternalSubscriptionStatus;
};

export type ExternalPollerIdentity = {
  hwid?: string | null;
  user_agent?: string | null;
};

const KEYS = {
  list: "external-subscriptions",
  poller: "external-subscriptions-poller",
};

/** Sudo-only on the backend; non-sudo callers get 403 and an empty list. */
export const useExternalSubscriptions = (enabled: boolean) =>
  useQuery<ExternalSubscription[]>(
    KEYS.list,
    () => fetch("/external-subscriptions"),
    { enabled, retry: false, refetchInterval: enabled ? 30000 : false }
  );

/** The poller's fallback identity — only used by rows that set no HWID. */
export const usePollerIdentity = (enabled: boolean) =>
  useQuery<ExternalPollerIdentity>(
    KEYS.poller,
    () => fetch("/external-subscriptions/poller"),
    { enabled, retry: false }
  );

export const useAddExternalSubscription = () => {
  const qc = useQueryClient();
  return useMutation<
    ExternalSubscription,
    Error,
    {
      name: string;
      url: string;
      hwid?: string | null;
      enabled?: boolean;
      admin_ids?: number[];
    }
  >({
    mutationFn: (body) => fetch("/external-subscriptions", { method: "POST", body }),
    onSuccess: () => qc.invalidateQueries(KEYS.list),
  });
};

/** Partial update. Send only what changes; `hwid: null` clears it explicitly
 * (the backend distinguishes omitted from null for that one field). */
export const useUpdateExternalSubscription = () => {
  const qc = useQueryClient();
  return useMutation<
    ExternalSubscription,
    Error,
    {
      id: number;
      body: Partial<
        Pick<ExternalSubscription, "name" | "url" | "hwid" | "enabled" | "admin_ids">
      >;
    }
  >({
    mutationFn: ({ id, body }) =>
      fetch(`/external-subscriptions/${id}`, { method: "PUT", body }),
    onSuccess: () => qc.invalidateQueries(KEYS.list),
  });
};

export const useDeleteExternalSubscription = () => {
  const qc = useQueryClient();
  return useMutation<any, Error, number>({
    mutationFn: (id) => fetch(`/external-subscriptions/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries(KEYS.list),
  });
};

/** Parse a `subscription-userinfo` header into something displayable.
 *
 * Shape (verified live): `upload=0; download=115114229; total=107374182400;
 * expire=1789295760`. `total=0` means unlimited, matching the panel's own
 * convention; a missing or zero `expire` means never.
 */
export const parseRemoteUserinfo = (raw?: string | null) => {
  if (!raw) return null;
  const parts: Record<string, number> = {};
  raw.split(";").forEach((chunk) => {
    const [k, v] = chunk.split("=").map((s) => s?.trim());
    if (k && v !== undefined && !Number.isNaN(Number(v))) parts[k] = Number(v);
  });
  const used = (parts.upload ?? 0) + (parts.download ?? 0);
  const total = parts.total ?? 0;
  const expire = parts.expire ?? 0;
  return {
    used,
    total,
    expire: expire > 0 ? new Date(expire * 1000) : null,
    daysLeft:
      expire > 0
        ? Math.floor((expire * 1000 - Date.now()) / 86400000)
        : null,
    percentUsed: total > 0 ? Math.min(100, (used / total) * 100) : null,
  };
};
