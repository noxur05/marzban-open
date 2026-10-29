import {
  Box,
  Flex,
  HStack,
  SimpleGrid,
  Skeleton,
  Text,
  useColorMode,
  VStack,
} from "@chakra-ui/react";
import { EmptyState, SegmentedControl } from "components/shared";
import { LoadError, RankedList, Sheet, StatTile } from "components/shared";
import { ClientAppsResponse, ClientAppStats } from "hooks/useClientApps";
import { FC, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  appColor,
  countOf,
  HAPP,
  MICKY,
  OTHER_APPS_COLOR,
  percent,
  platformTotals,
} from "utils/clientApps";
import { numberWithCommas } from "utils/formatByte";

/** Rows of a list on the page; the rest is one tap away in a sheet. */
const LIST_ROWS = 5;

type Label = (name: string | null) => string;

/**
 * Happ / Micky / every other app as one part-to-whole bar, with each part's
 * share and count as labelled values under it (the bar's only legend).
 */
const ShareBar: FC<{ data: ClientAppsResponse; dark: boolean }> = ({
  data,
  dark,
}) => {
  const { t } = useTranslation();
  const happ = countOf(data.clients, HAPP);
  const micky = countOf(data.clients, MICKY);
  const parts = [
    { key: HAPP, label: HAPP, n: happ, color: appColor(HAPP, dark) },
    { key: MICKY, label: MICKY, n: micky, color: appColor(MICKY, dark) },
    {
      key: "other",
      label: t("statistics.otherApps"),
      n: data.devices - happ - micky,
      color: OTHER_APPS_COLOR,
    },
  ];
  return (
    <Box>
      <Flex
        h="12px"
        gap="2px"
        borderRadius="4px"
        overflow="hidden"
        role="img"
        aria-label={t("statistics.shareOfDevices")}
      >
        {parts
          .filter((p) => p.n > 0)
          .map((p) => (
            <Box key={p.key} flex={`${p.n} 1 0`} minW="3px" bg={p.color} />
          ))}
      </Flex>
      <SimpleGrid columns={3} spacing={2} mt={2}>
        {parts.map((p) => (
          <Box key={p.key} minW={0}>
            <HStack spacing={1.5}>
              <Box
                flex="none"
                w="10px"
                h="10px"
                borderRadius="2px"
                bg={p.color}
              />
              <Text
                fontSize="xs"
                noOfLines={1}
                color="gray.600"
                _dark={{ color: "gray.400" }}
              >
                {p.label}
              </Text>
            </HStack>
            <Text
              fontSize="md"
              fontWeight="semibold"
              sx={{ fontVariantNumeric: "tabular-nums" }}
            >
              {percent(p.n, data.devices)}
            </Text>
            <Text
              fontSize="xs"
              color="gray.500"
              _dark={{ color: "gray.400" }}
              sx={{ fontVariantNumeric: "tabular-nums" }}
            >
              {numberWithCommas(p.n)}
            </Text>
          </Box>
        ))}
      </SimpleGrid>
    </Box>
  );
};

/** One app's details: its numbers, platforms, and (sudo, all admins) who uses it. */
const AppDetails: FC<{
  app: ClientAppStats;
  data: ClientAppsResponse;
  showAdmins: boolean;
  label: Label;
  dark: boolean;
}> = ({ app, data, showAdmins, label, dark }) => {
  const { t } = useTranslation();
  const admins = data.admins
    .map((a) => ({ a, n: countOf(a.clients, app.name) }))
    .filter((x) => x.n > 0)
    .sort((x, y) => y.n - x.n);
  return (
    <VStack align="stretch" spacing={5}>
      <SimpleGrid columns={3} spacing={2}>
        <StatTile
          label={t("adminsUsage.metric.devices")}
          value={numberWithCommas(app.devices)}
          caption={percent(app.devices, data.devices)}
        />
        <StatTile
          label={t("adminsUsage.metric.users")}
          value={numberWithCommas(app.users)}
        />
        <StatTile
          label={t("statistics.firstSeen")}
          value={numberWithCommas(app.new_devices)}
        />
      </SimpleGrid>
      <Box>
        <Text fontSize="sm" fontWeight="semibold" mb={1}>
          {t("statistics.platforms")}
        </Text>
        <RankedList
          items={app.platforms.map((p) => ({
            key: p.name ?? "",
            title: label(p.name),
            value: numberWithCommas(p.devices),
            secondary: percent(p.devices, app.devices),
            bar: p.devices / (app.platforms[0]?.devices || 1),
          }))}
          barColor={appColor(app.name, dark)}
        />
      </Box>
      {showAdmins && admins.length > 0 && (
        <Box>
          <Text fontSize="sm" fontWeight="semibold" mb={1}>
            {t("statistics.tabAdmins")}
          </Text>
          <RankedList
            ranked
            header={{
              title: t("adminsUsage.admin"),
              value: label(app.name),
              secondary: t("statistics.ofAdmin"),
            }}
            items={admins.map(({ a, n }) => ({
              key: a.username ?? "",
              title: a.username ?? t("statistics.noAdmin"),
              value: numberWithCommas(n),
              secondary: percent(n, a.devices),
            }))}
          />
        </Box>
      )}
    </VStack>
  );
};

type ClientAppsProps = {
  data: ClientAppsResponse | undefined;
  isError: boolean;
  onRetry: () => void;
  /** Sudo viewing every admin: app details list the admins using the app. */
  showAdmins: boolean;
};

type Breakdown = "apps" | "platforms";

/**
 * The Statistics page's Apps tab: which client apps (Happ, Micky, V2Box, ...)
 * the devices in use during the range run. Headline tiles, the Happ / Micky /
 * other split, then one breakdown at a time (apps or platforms) as a top-5
 * list; "View all" and a tap on an app open sheets, so the tab stays one
 * phone screen whatever the data.
 */
export const ClientApps: FC<ClientAppsProps> = ({
  data,
  isError,
  onRetry,
  showAdmins,
}) => {
  const { t } = useTranslation();
  const dark = useColorMode().colorMode === "dark";
  const [breakdown, setBreakdown] = useState<Breakdown>("apps");
  const [sheet, setSheet] = useState<
    | { kind: "apps" }
    | { kind: "platforms" }
    | { kind: "app"; name: string | null }
    | null
  >(null);
  const label: Label = (name) => name ?? t("statistics.unknownApp");

  if (!data && isError) {
    return (
      <LoadError message={t("adminsUsage.loadError")} onRetry={onRetry} />
    );
  }
  if (!data) {
    return (
      <VStack align="stretch" spacing={4}>
        <SimpleGrid columns={{ base: 2, md: 4 }} spacing={2}>
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} h="76px" borderRadius="lg" />
          ))}
        </SimpleGrid>
        <Skeleton h="300px" borderRadius="lg" />
      </VStack>
    );
  }
  if (data.devices === 0)
    return <EmptyState compact title={t("statistics.noClientApps")} />;

  const micky = data.clients.find((c) => c.name === MICKY);
  const platforms = platformTotals(data.clients);
  // "Unknown" sorts last whatever its size, so the scale is the largest count, not the first row.
  const platformMax = Math.max(1, ...platforms.map((p) => p.devices));
  const appItems = (rows: ClientAppStats[]) =>
    rows.map((c) => ({
      key: c.name ?? "",
      title: label(c.name),
      swatch: appColor(c.name, dark),
      value: numberWithCommas(c.devices),
      secondary: percent(c.devices, data.devices),
      onClick: () => setSheet({ kind: "app", name: c.name }),
    }));
  const platformItems = platforms.map((p) => ({
    key: p.name ?? "",
    title: label(p.name),
    value: numberWithCommas(p.devices),
    secondary: percent(p.devices, data.devices),
    bar: p.devices / platformMax,
  }));
  const openApp =
    sheet?.kind === "app"
      ? data.clients.find((c) => c.name === sheet.name)
      : undefined;
  const listHeader = (title: string) => ({
    title,
    value: t("adminsUsage.metric.devices"),
    secondary: t("statistics.share"),
  });

  return (
    <VStack align="stretch" spacing={5}>
      <SimpleGrid columns={{ base: 2, md: 4 }} spacing={2}>
        <StatTile
          label={t("adminsUsage.metric.devices")}
          value={numberWithCommas(data.devices)}
          caption={t("statistics.inUseInRange")}
          info={{
            label: t("statistics.aboutDevicesInUse"),
            text: t("statistics.devicesInUseInfo"),
          }}
        />
        <StatTile
          label={t("adminsUsage.metric.users")}
          value={numberWithCommas(data.users)}
          caption={t("statistics.withADevice")}
        />
        <StatTile
          label={t("statistics.firstSeen")}
          value={numberWithCommas(data.new_devices)}
          caption={t("statistics.inRange")}
        />
        <StatTile
          label={MICKY}
          value={numberWithCommas(micky?.devices ?? 0)}
          caption={t("statistics.ofAllDevices", {
            share: percent(micky?.devices ?? 0, data.devices),
          })}
          accent={appColor(MICKY, dark)}
          onClick={
            micky ? () => setSheet({ kind: "app", name: MICKY }) : undefined
          }
        />
      </SimpleGrid>

      <ShareBar data={data} dark={dark} />

      <Box>
        <SegmentedControl<Breakdown>
          value={breakdown}
          onChange={setBreakdown}
          mb={3}
          options={[
            { value: "apps", label: t("statistics.apps") },
            { value: "platforms", label: t("statistics.platforms") },
          ]}
        />
        {breakdown === "apps" ? (
          <RankedList
            header={listHeader(t("statistics.app"))}
            items={appItems(data.clients)}
            limit={LIST_ROWS}
            viewAll={{
              label: t("statistics.viewAllApps", {
                count: data.clients.length,
              }),
              onClick: () => setSheet({ kind: "apps" }),
            }}
          />
        ) : (
          <RankedList
            header={listHeader(t("statistics.platform"))}
            items={platformItems}
            limit={LIST_ROWS}
            barColor="gray.500"
            viewAll={{
              label: t("statistics.viewAll"),
              onClick: () => setSheet({ kind: "platforms" }),
            }}
          />
        )}
      </Box>

      <Sheet
        isOpen={sheet?.kind === "apps"}
        onClose={() => setSheet(null)}
        title={t("statistics.allApps", { count: data.clients.length })}
      >
        <RankedList
          header={listHeader(t("statistics.app"))}
          items={appItems(data.clients)}
        />
      </Sheet>
      <Sheet
        isOpen={sheet?.kind === "platforms"}
        onClose={() => setSheet(null)}
        title={t("statistics.platforms")}
      >
        <RankedList
          header={listHeader(t("statistics.platform"))}
          items={platformItems}
          barColor="gray.500"
        />
      </Sheet>
      <Sheet
        isOpen={!!openApp}
        onClose={() => setSheet(null)}
        title={
          openApp && (
            <HStack spacing={2}>
              <Box
                w="12px"
                h="12px"
                borderRadius="3px"
                bg={appColor(openApp.name, dark)}
              />
              <Text as="span">{label(openApp.name)}</Text>
            </HStack>
          )
        }
      >
        {openApp && (
          <AppDetails
            app={openApp}
            data={data}
            showAdmins={showAdmins}
            label={label}
            dark={dark}
          />
        )}
      </Sheet>
    </VStack>
  );
};
