import { useMutation, useQuery, useQueryClient } from "react-query";
import { fetch } from "service/http";

/** An operator-authored proxy host — a pasted share link scoped to admins.
 * The manual, poller-free twin of ExternalSubscription. */
export type ManualHost = {
  id: number;
  name: string;
  link: string;
  enabled: boolean;
  /** Admins whose users receive this host. EMPTY = nobody, never everybody —
   * "scope to specific admins" fails closed. */
  admin_ids: number[];
  created_at: string;
};

const KEYS = {
  list: "manual-hosts",
};

/** Sudo-only on the backend; non-sudo callers get 403 and an empty list. */
export const useManualHosts = (enabled: boolean) =>
  useQuery<ManualHost[]>(KEYS.list, () => fetch("/manual-hosts"), {
    enabled,
    retry: false,
  });

export const useAddManualHost = () => {
  const qc = useQueryClient();
  return useMutation<
    ManualHost,
    Error,
    { name: string; link: string; enabled?: boolean; admin_ids?: number[] }
  >({
    mutationFn: (body) => fetch("/manual-hosts", { method: "POST", body }),
    onSuccess: () => qc.invalidateQueries(KEYS.list),
  });
};

/** Partial update. Send only what changes; `admin_ids: []` clears every
 * assignment, omitting it leaves them alone. */
export const useUpdateManualHost = () => {
  const qc = useQueryClient();
  return useMutation<
    ManualHost,
    Error,
    {
      id: number;
      body: Partial<Pick<ManualHost, "name" | "link" | "enabled" | "admin_ids">>;
    }
  >({
    mutationFn: ({ id, body }) =>
      fetch(`/manual-hosts/${id}`, { method: "PUT", body }),
    onSuccess: () => qc.invalidateQueries(KEYS.list),
  });
};

export const useDeleteManualHost = () => {
  const qc = useQueryClient();
  return useMutation<any, Error, number>({
    mutationFn: (id) => fetch(`/manual-hosts/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries(KEYS.list),
  });
};
