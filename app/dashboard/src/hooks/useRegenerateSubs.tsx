import { useMutation } from "react-query";
import { fetch } from "service/http";

/** Bulk-regenerate Happ and Micky subscription links (whole panel, or one
 * admin's) from each admin's current domain. Runs in the background server-side;
 * resolves as soon as the job is queued. */
export const useRegenerateSubs = () =>
  useMutation<{ queued: number }, Error, { admin_username?: string }>({
    mutationFn: ({ admin_username }) =>
      fetch(
        `/users/regenerate-subs${
          admin_username ? `?admin_username=${encodeURIComponent(admin_username)}` : ""
        }`,
        { method: "POST" }
      ),
  });

/** Regenerate one Happ or Micky user's link. Synchronous, computed locally. */
export const useRegenerateUserSub = () =>
  useMutation<any, Error, string>({
    mutationFn: (username) =>
      fetch(`/user/${username}/regenerate-sub`, { method: "POST" }),
  });
