import { Box, BoxProps, Card, chakra, HStack, Text } from "@chakra-ui/react";
import {
  ChartBarIcon,
  ChartPieIcon,
  DevicePhoneMobileIcon,
  UsersIcon,
} from "@heroicons/react/24/outline";
import { useDashboard } from "contexts/DashboardContext";
import {
  DeviceStatsQueryKey,
  StatisticsQueryKey,
  TopUsersQueryKey,
} from "constants/queryKeys";
import { FC, PropsWithChildren, ReactElement, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "react-query";
import { fetch } from "service/http";
import { formatBytes, numberWithCommas } from "utils/formatByte";

const TotalUsersIcon = chakra(UsersIcon, {
  baseStyle: {
    w: 5,
    h: 5,
    position: "relative",
    zIndex: "2",
  },
});

const NetworkIcon = chakra(ChartBarIcon, {
  baseStyle: {
    w: 5,
    h: 5,
    position: "relative",
    zIndex: "2",
  },
});

const MemoryIcon = chakra(ChartPieIcon, {
  baseStyle: {
    w: 5,
    h: 5,
    position: "relative",
    zIndex: "2",
  },
});

const DevicesIcon = chakra(DevicePhoneMobileIcon, {
  baseStyle: {
    w: 5,
    h: 5,
    position: "relative",
    zIndex: "2",
  },
});

type DeviceStats = {
  active_users_devices: number;
  total_devices: number;
};

export type StatisticCardProps = {
  title: string;
  content: ReactNode;
  icon: ReactElement;
};

export const StatisticCard: FC<PropsWithChildren<StatisticCardProps>> = ({
  title,
  content,
  icon,
}) => {
  return (
    <Card
      p={6}
      borderWidth="1px"
      borderColor="light-border"
      bg="#F9FAFB"
      _dark={{ borderColor: "gray.600", bg: "gray.750" }}
      borderStyle="solid"
      boxShadow="none"
      borderRadius="12px"
      width="full"
      display="flex"
      justifyContent="space-between"
      flexDirection="row"
    >
      <HStack alignItems="center" columnGap="4">
        <Box
          p="2"
          position="relative"
          color="white"
          _before={{
            content: `""`,
            position: "absolute",
            top: 0,
            left: 0,
            bg: "primary.400",
            display: "block",
            w: "full",
            h: "full",
            borderRadius: "5px",
            opacity: ".5",
            z: "1",
          }}
          _after={{
            content: `""`,
            position: "absolute",
            top: "-5px",
            left: "-5px",
            bg: "primary.400",
            display: "block",
            w: "calc(100% + 10px)",
            h: "calc(100% + 10px)",
            borderRadius: "8px",
            opacity: ".4",
            z: "1",
          }}
        >
          {icon}
        </Box>
        <Text
          color="gray.600"
          _dark={{
            color: "gray.300",
          }}
          fontWeight="medium"
          textTransform="capitalize"
          fontSize="sm"
        >
          {title}
        </Text>
      </HStack>
      <Box fontSize="3xl" fontWeight="semibold" mt="2">
        {content}
      </Box>
    </Card>
  );
};
// Declared in constants/queryKeys so a hook that only needs to invalidate a
// card does not have to import this whole component module. Re-exported here
// because several call sites still import them from this path.
// TopUsersQueryKey drives the Statistics page's Users tab (hooks/useTopUsers);
// it is invalidated wherever device counts change so "Most devices" never lags
// the users table. ClientAppsQueryKey (the Apps tab) is invalidated with it.
export {
  DeviceStatsQueryKey,
  StatisticsQueryKey,
  TopUsersQueryKey,
};
export const Statistics: FC<BoxProps> = (props) => {
  const { version, filters } = useDashboard();
  const { data: systemData } = useQuery({
    queryKey: StatisticsQueryKey,
    queryFn: () => fetch("/system"),
    refetchInterval: 5000,
    onSuccess: ({ version: currentVersion }) => {
      if (version !== currentVersion)
        useDashboard.setState({ version: currentVersion });
    },
  });
  // Re-fetch when the sudo admin filter changes so the Active Devices
  // card reflects the same scope as the users list below. Non-sudo
  // admins always see their own users — server ignores the param.
  const adminFilter = filters.admin || "";
  const { data: deviceStats } = useQuery<DeviceStats>({
    queryKey: [DeviceStatsQueryKey, adminFilter],
    queryFn: () =>
      fetch("/system/devices", {
        query: adminFilter ? { admin_username: adminFilter } : undefined,
      }),
    refetchInterval: 5000,
  });
  const { t } = useTranslation();
  return (
    <HStack
      justifyContent="space-between"
      gap={0}
      columnGap={{ lg: 4, md: 0 }}
      rowGap={{ lg: 0, base: 4 }}
      display="flex"
      flexDirection={{ lg: "row", base: "column" }}
      {...props}
    >
      <StatisticCard
        title={t("activeUsers")}
        content={
          systemData && (
            <HStack alignItems="flex-end">
              <Text>{numberWithCommas(systemData.users_active)}</Text>
              <Text
                fontWeight="normal"
                fontSize="lg"
                as="span"
                display="inline-block"
                pb="5px"
              >
                / {numberWithCommas(systemData.total_user)}
              </Text>
            </HStack>
          )
        }
        icon={<TotalUsersIcon />}
      />
      <StatisticCard
        title={t("activeDevices")}
        content={
          deviceStats && (
            <HStack alignItems="flex-end">
              <Text>{numberWithCommas(deviceStats.active_users_devices)}</Text>
              <Text
                fontWeight="normal"
                fontSize="lg"
                as="span"
                display="inline-block"
                pb="5px"
              >
                / {numberWithCommas(deviceStats.total_devices)}
              </Text>
            </HStack>
          )
        }
        icon={<DevicesIcon />}
      />
      <StatisticCard
        title={t("dataUsage")}
        content={
          systemData &&
          formatBytes(
            systemData.incoming_bandwidth + systemData.outgoing_bandwidth
          )
        }
        icon={<NetworkIcon />}
      />
      <StatisticCard
        title={t("memoryUsage")}
        content={
          systemData && (
            <HStack alignItems="flex-end">
              <Text>{formatBytes(systemData.mem_used, 1, true)[0]}</Text>
              <Text
                fontWeight="normal"
                fontSize="lg"
                as="span"
                display="inline-block"
                pb="5px"
              >
                {formatBytes(systemData.mem_used, 1, true)[1]} /{" "}
                {formatBytes(systemData.mem_total, 1)}
              </Text>
            </HStack>
          )
        }
        icon={<MemoryIcon />}
      />
    </HStack>
  );
};
