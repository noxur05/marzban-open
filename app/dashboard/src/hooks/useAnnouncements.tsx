import { fetch } from "service/http";
import { useMutation, useQuery, useQueryClient } from "react-query";

export type Announcement = {
  id: number;
  admin_id: number | null;
  text: string;
  enabled: boolean;
  created_at: string;
  created_by_admin_id: number | null;
};

export type AnnouncementOverviewItem = {
  admin_id: number | null;
  username: string | null;
  can_announce: boolean | null;
  latest: Announcement | null;
};

export type AnnouncementSet = { text: string; enabled: boolean };

export const ANNOUNCEMENT_KEYS = {
  mine: ["announcement", "mine"] as const,
  overview: ["announcement", "overview"] as const,
  globalHistory: ["announcement", "history", "global"] as const,
  adminHistory: (username: string) =>
    ["announcement", "history", "admin", username] as const,
};

type QC = ReturnType<typeof useQueryClient>;
// Each mutation refetches only the queries it can have changed. The overview
// is included everywhere a sudo could be looking at it, because it lists
// every row's latest announcement.
const invalidateMine = (qc: QC) => {
  qc.invalidateQueries(ANNOUNCEMENT_KEYS.mine);
  qc.invalidateQueries(ANNOUNCEMENT_KEYS.overview);
};
const invalidateGlobal = (qc: QC) => {
  qc.invalidateQueries(ANNOUNCEMENT_KEYS.overview);
  qc.invalidateQueries(ANNOUNCEMENT_KEYS.globalHistory);
};
const invalidateAdmin = (qc: QC, username: string) => {
  qc.invalidateQueries(ANNOUNCEMENT_KEYS.overview);
  qc.invalidateQueries(ANNOUNCEMENT_KEYS.adminHistory(username));
};

// ---- self (any admin) ----
export const useMyAnnouncements = (enabled: boolean = true) =>
  useQuery<Announcement[], Error>({
    queryKey: ANNOUNCEMENT_KEYS.mine,
    queryFn: () => fetch("/announcement"),
    enabled,
  });

export const useSetMyAnnouncement = () => {
  const qc = useQueryClient();
  return useMutation<Announcement, Error, AnnouncementSet>({
    mutationFn: (body) => fetch("/announcement", { method: "PUT", body }),
    onSuccess: () => invalidateMine(qc),
  });
};

export const useDisableMyAnnouncement = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, void>({
    mutationFn: () => fetch("/announcement", { method: "DELETE" }),
    onSuccess: () => invalidateMine(qc),
  });
};

// ---- sudo: overview + global + per-admin ----
export const useAnnouncementsOverview = (enabled: boolean) =>
  useQuery<AnnouncementOverviewItem[], Error>({
    queryKey: ANNOUNCEMENT_KEYS.overview,
    queryFn: () => fetch("/announcements"),
    enabled,
    retry: false,
  });

export const useGlobalAnnouncementHistory = (enabled: boolean) =>
  useQuery<Announcement[], Error>({
    queryKey: ANNOUNCEMENT_KEYS.globalHistory,
    queryFn: () => fetch("/announcements/global"),
    enabled,
    retry: false,
  });

export const useAdminAnnouncementHistory = (username: string | null) =>
  useQuery<Announcement[], Error>({
    queryKey: ANNOUNCEMENT_KEYS.adminHistory(username ?? ""),
    queryFn: () => fetch(`/admin/${encodeURIComponent(username ?? "")}/announcements`),
    enabled: !!username,
    retry: false,
  });

export const useSetGlobalAnnouncement = () => {
  const qc = useQueryClient();
  return useMutation<Announcement, Error, AnnouncementSet>({
    mutationFn: (body) => fetch("/announcement/global", { method: "PUT", body }),
    onSuccess: () => invalidateGlobal(qc),
  });
};

export const useDisableGlobalAnnouncement = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, void>({
    mutationFn: () => fetch("/announcement/global", { method: "DELETE" }),
    onSuccess: () => invalidateGlobal(qc),
  });
};

export const useSetAdminAnnouncement = () => {
  const qc = useQueryClient();
  return useMutation<Announcement, Error, { username: string; body: AnnouncementSet }>({
    mutationFn: ({ username, body }) =>
      fetch(`/admin/${encodeURIComponent(username)}/announcement`, { method: "PUT", body }),
    onSuccess: (_data, { username }) => invalidateAdmin(qc, username),
  });
};

export const useDisableAdminAnnouncement = () => {
  const qc = useQueryClient();
  return useMutation<unknown, Error, string>({
    mutationFn: (username) =>
      fetch(`/admin/${encodeURIComponent(username)}/announcement`, { method: "DELETE" }),
    onSuccess: (_data, username) => invalidateAdmin(qc, username),
  });
};
