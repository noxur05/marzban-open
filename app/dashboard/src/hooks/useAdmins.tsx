import { useQuery } from "react-query";
import { fetch } from "service/http";

export type AdminListItem = {
  id: number | null;
  username: string;
  is_sudo: boolean;
  // The /admins endpoint returns full Admin objects; these flags are used
  // to preserve permissions when toggling one of them via AdminModify.
  telegram_id?: number | null;
  discord_webhook?: string | null;
  subscription_url?: string | null;
  can_announce?: boolean;
  can_enable_outline?: boolean;
  happ_only?: boolean;
  /** Denormalised total traffic of this admin's users, in bytes. */
  users_usage?: number | null;
  /** Users this admin owns, any status (`GET /api/admins` only). */
  users_count?: number;
};

export const ADMINS_QUERY_KEY = "admins";

const fetchAdmins = (): Promise<AdminListItem[]> => fetch("/admins");

/**
 * Fetches all admins. Sudo-only on the backend; non-sudo callers
 * receive 403 and the hook returns an empty list. Cached for 5 minutes;
 * `fresh` refetches on mount anyway (the Admins page: user counts move with
 * every user created or deleted, which does not refresh this list).
 */
export const useAdmins = (enabled: boolean, fresh = false) => {
  const { data, isLoading, isError, isFetching, refetch } = useQuery<AdminListItem[]>(
    ADMINS_QUERY_KEY,
    fetchAdmins,
    {
      enabled,
      staleTime: 5 * 60 * 1000,
      retry: false,
      refetchOnMount: fresh ? "always" : true,
    }
  );
  return {
    admins: data ?? [],
    isLoadingAdmins: isLoading,
    isErrorAdmins: isError,
    isFetchingAdmins: isFetching,
    refetchAdmins: refetch,
  };
};

export default useAdmins;
