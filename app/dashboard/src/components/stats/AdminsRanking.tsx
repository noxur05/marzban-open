import {
  Box,
  Button,
  SimpleGrid,
  Skeleton,
  Text,
  useColorMode,
  VStack,
} from "@chakra-ui/react";
import { EmptyState, SegmentedControl } from "components/shared";
import { ClientAppsResponse } from "hooks/useClientApps";
import { FC, useState } from "react";
import { useTranslation } from "react-i18next";
import { appColor, countOf, MICKY, percent } from "utils/clientApps";
import { formatBytes, numberWithCommas } from "utils/formatByte";
import { AdminRow } from "utils/usageChart";
import { LoadError, RankedList, Sheet, StatTile } from "components/shared";

/** Admins listed on the tab; the rest is behind "View all". */
const LIST_ROWS = 8;

type Rank = "traffic" | "devices" | "users" | "micky";

type AdminsRankingProps = {
  /** Per-admin usage over the range (traffic sum, gauge averages). */
  rows: AdminRow[] | undefined;
  apps: ClientAppsResponse | undefined;
  /** The usage / client-apps query failed with nothing to show. */
  usageError: boolean;
  appsError: boolean;
  onRetry: () => void;
  /** Filter the whole page to one admin. */
  onSelectAdmin: (username: string) => void;
};

/**
 * The Statistics page's Admins tab (sudo, every admin in view): the admins
 * ranked by one metric at a time — traffic, devices, users or Micky adoption
 * — instead of a wide table. A row opens that admin's numbers and app mix,
 * with one tap to filter the whole page to them.
 */
export const AdminsRanking: FC<AdminsRankingProps> = ({
  rows,
  apps,
  usageError,
  appsError,
  onRetry,
  onSelectAdmin,
}) => {
  const { t } = useTranslation();
  const dark = useColorMode().colorMode === "dark";
  const [rank, setRank] = useState<Rank>("traffic");
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  const appsOf = (username: string) =>
    apps?.admins.find((a) => a.username === username);
  const micky = (username: string) => {
    const a = appsOf(username);
    return a
      ? { n: countOf(a.clients, MICKY), of: a.devices }
      : { n: 0, of: 0 };
  };
  const value = (r: AdminRow) =>
    rank === "traffic"
      ? r.total_traffic
      : rank === "devices"
      ? r.avgDevices
      : rank === "users"
      ? r.avgUsers
      : micky(r.username).n;
  const ready = !!rows && (rank !== "micky" || !!apps);
  const sorted = ready
    ? [...rows!].sort(
        (a, b) => value(b) - value(a) || a.username.localeCompare(b.username)
      )
    : [];
  const total = sorted.reduce((s, r) => s + value(r), 0);
  const max = sorted.length ? value(sorted[0]) || 1 : 1;

  const items = sorted.map((r) => {
    const v = value(r);
    const m = micky(r.username);
    return {
      key: String(r.admin_id),
      title: r.username,
      value: rank === "traffic" ? formatBytes(v) : numberWithCommas(v),
      // Micky: share of the admin's own devices; otherwise share of the total.
      secondary: rank === "micky" ? percent(m.n, m.of) : percent(v, total),
      bar: v / max,
      onClick: () => setOpen(r.username),
    };
  });
  const header = {
    title: t("adminsUsage.admin"),
    value:
      rank === "traffic"
        ? t("adminsUsage.metric.traffic")
        : rank === "devices"
        ? t("statistics.avgDevicesShort")
        : rank === "users"
        ? t("statistics.avgUsersShort")
        : MICKY,
    secondary:
      rank === "micky" ? t("statistics.ofAdmin") : t("statistics.share"),
  };
  const list = (limit?: number) => (
    <RankedList
      ranked
      header={header}
      items={items}
      limit={limit}
      valueWidth={rank === "traffic" ? "84px" : "56px"}
      barColor={rank === "micky" ? appColor(MICKY, dark) : "primary.500"}
      viewAll={
        limit
          ? {
              label: t("statistics.viewAllAdmins", { count: items.length }),
              onClick: () => setAll(true),
            }
          : undefined
      }
    />
  );

  const row = open ? rows?.find((r) => r.username === open) : undefined;
  const mix = open ? appsOf(open) : undefined;
  const m = open ? micky(open) : { n: 0, of: 0 };

  return (
    <VStack align="stretch" spacing={3}>
      <Box overflowX="auto" mx={-1} px={1}>
        <SegmentedControl<Rank>
          value={rank}
          onChange={setRank}
          options={[
            { value: "traffic", label: t("adminsUsage.metric.traffic") },
            { value: "devices", label: t("adminsUsage.metric.devices") },
            { value: "users", label: t("adminsUsage.metric.users") },
            { value: "micky", label: MICKY },
          ]}
        />
      </Box>
      {/* Only an error in what the selected ranking needs: traffic, devices
          and users come from usage, Micky also needs the app data. */}
      {!ready && (usageError || (rank === "micky" && appsError)) ? (
        <LoadError message={t("adminsUsage.loadError")} onRetry={onRetry} />
      ) : !ready ? (
        <Skeleton h="380px" borderRadius="lg" />
      ) : items.length === 0 ? (
        <EmptyState compact title={t("adminsUsage.noData")} />
      ) : (
        list(LIST_ROWS)
      )}

      <Sheet
        isOpen={all}
        onClose={() => setAll(false)}
        title={t("statistics.tabAdmins")}
      >
        {list()}
      </Sheet>

      <Sheet isOpen={!!open} onClose={() => setOpen(null)} title={open}>
        {row && (
          <VStack align="stretch" spacing={5}>
            <SimpleGrid columns={2} spacing={2}>
              <StatTile
                label={t("adminsUsage.metric.traffic")}
                value={formatBytes(row.total_traffic)}
                caption={t("statistics.inRange")}
              />
              <StatTile
                label={t("adminsUsage.metric.devices")}
                value={numberWithCommas(row.avgDevices)}
                caption={t("statistics.dailyAvg")}
              />
              <StatTile
                label={t("adminsUsage.metric.users")}
                value={numberWithCommas(row.avgUsers)}
                caption={t("statistics.dailyAvg")}
              />
              <StatTile
                label={MICKY}
                value={apps ? numberWithCommas(m.n) : "—"}
                caption={
                  apps &&
                  t("statistics.ofTheirDevices", {
                    share: percent(m.n, m.of),
                  })
                }
                accent={appColor(MICKY, dark)}
                isLoading={!apps && !appsError}
              />
            </SimpleGrid>
            {mix && mix.clients.length > 0 && (
              <Box>
                <Text fontSize="sm" fontWeight="semibold" mb={1}>
                  {t("statistics.apps")}
                </Text>
                <RankedList
                  header={{
                    title: t("statistics.app"),
                    value: t("adminsUsage.metric.devices"),
                    secondary: t("statistics.share"),
                  }}
                  items={mix.clients.map((c) => ({
                    key: c.name ?? "",
                    title: c.name ?? t("statistics.unknownApp"),
                    swatch: appColor(c.name, dark),
                    value: numberWithCommas(c.devices),
                    secondary: percent(c.devices, mix.devices),
                  }))}
                />
              </Box>
            )}
            <Button
              colorScheme="primary"
              minH="44px"
              onClick={() => {
                onSelectAdmin(row.username);
                setOpen(null);
              }}
            >
              {t("statistics.showAdminStats", { admin: row.username })}
            </Button>
          </VStack>
        )}
      </Sheet>
    </VStack>
  );
};
