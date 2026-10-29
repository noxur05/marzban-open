import {
  Skeleton,
  Text,
  VStack,
} from "@chakra-ui/react";
import { EmptyState, SegmentedControl } from "components/shared";
import { LoadError, RankedList } from "components/shared";
import { TopUsersResponse } from "hooks/useTopUsers";
import { FC, useState } from "react";
import { useTranslation } from "react-i18next";
import { formatBytes, numberWithCommas } from "utils/formatByte";

type TopUsersProps = {
  data: TopUsersResponse | undefined;
  isError: boolean;
  onRetry: () => void;
  /** Sudo viewing every admin: show whose user each row is. */
  showOwner: boolean;
};

type Ranking = "devices" | "traffic";

/**
 * The Statistics page's Users tab: one ranked Top-10 at a time — most
 * active devices right now, or most traffic inside the range — switched by a
 * segmented control instead of two lists side by side, so it fits a phone.
 *
 * A failed refetch keeps the last good list on screen; the error banner only
 * appears when there is nothing to show.
 */
export const TopUsers: FC<TopUsersProps> = ({
  data,
  isError,
  onRetry,
  showOwner,
}) => {
  const { t } = useTranslation();
  const [ranking, setRanking] = useState<Ranking>("devices");

  if (!data && isError) {
    return (
      <LoadError message={t("adminsUsage.loadError")} onRetry={onRetry} />
    );
  }

  const rows = ranking === "devices" ? data?.by_devices : data?.by_traffic;
  const max = rows?.[0]?.value || 1;
  return (
    <VStack align="stretch" spacing={3}>
      <SegmentedControl<Ranking>
        value={ranking}
        onChange={setRanking}
        options={[
          { value: "devices", label: t("statistics.mostDevices") },
          { value: "traffic", label: t("statistics.mostTraffic") },
        ]}
      />
      {!rows ? (
        <Skeleton h="320px" borderRadius="lg" />
      ) : rows.length === 0 ? (
        <EmptyState
          compact
          title={
            ranking === "devices"
              ? t("statistics.noTopDevices")
              : t("statistics.noTopTraffic")
          }
        />
      ) : (
        <RankedList
          ranked
          valueWidth={ranking === "traffic" ? "84px" : "48px"}
          header={{
            title: t("username"),
            value:
              ranking === "devices"
                ? t("adminsUsage.metric.devices")
                : t("adminsUsage.metric.traffic"),
          }}
          items={rows.map((r) => ({
            key: r.username,
            title: r.username,
            subtitle: showOwner
              ? r.admin ?? t("statistics.noAdmin")
              : undefined,
            value:
              ranking === "devices"
                ? numberWithCommas(r.value)
                : formatBytes(r.value),
            bar: r.value / max,
          }))}
        />
      )}
      <Text fontSize="xs" color="gray.500" _dark={{ color: "gray.400" }}>
        {ranking === "devices"
          ? t("statistics.devicesNow")
          : t("statistics.trafficInRange")}
      </Text>
    </VStack>
  );
};
