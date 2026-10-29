import dayjs, { Dayjs } from "dayjs";

/** The panel stores and returns naive UTC timestamps (no `Z`). */
export const fromServerUtc = (iso: string | null | undefined): Dayjs | null =>
  iso ? dayjs.utc(iso).local() : null;

export const formatServerDateTime = (iso: string | null | undefined): string =>
  fromServerUtc(iso)?.format("YYYY-MM-DD HH:mm") ?? "";

/** Localized "3 hours ago" — follows the i18next language via dayjs.locale(). */
export const relativeFromServer = (iso: string | null | undefined): string =>
  fromServerUtc(iso)?.fromNow() ?? "";

/** Admin usage is bucketed by Asia/Ashgabat local day (UTC+5, no DST). */
export const ASHGABAT_TZ = "Asia/Ashgabat";
export const ashgabatToday = (): Dayjs => dayjs().tz(ASHGABAT_TZ);
