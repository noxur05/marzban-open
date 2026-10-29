import {
  Alert,
  AlertIcon,
  Badge,
  Box,
  Button,
  Code,
  HStack,
  Skeleton,
  Text,
  Tooltip,
  VStack,
} from "@chakra-ui/react";
import {
  ComputerDesktopIcon,
  DevicePhoneMobileIcon,
  DeviceTabletIcon,
  NoSymbolIcon,
  QuestionMarkCircleIcon,
  ShieldExclamationIcon,
} from "@heroicons/react/24/outline";
import { ConfirmModal, EmptyState, Hint, ModalActions, ModalShell } from "components/shared";
import { useDashboard } from "contexts/DashboardContext";
import { useAppToast } from "hooks/useAppToast";
import useGetUser from "hooks/useGetUser";
import { Device, useBlockDevice, useResetUserDevices, useUserDevices } from "hooks/useUserDevices";
import { FC, ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { User } from "types/User";
import { formatServerDateTime, relativeFromServer } from "utils/dates";

const platformIcon = (platform: string | null): ReactNode => {
  const p = (platform ?? "").toLowerCase();
  if (/ios|iphone|android/.test(p)) return <DevicePhoneMobileIcon width={18} height={18} />;
  if (/ipad|tablet/.test(p)) return <DeviceTabletIcon width={18} height={18} />;
  if (/win|mac|linux|desktop/.test(p)) return <ComputerDesktopIcon width={18} height={18} />;
  return <QuestionMarkCircleIcon width={18} height={18} />;
};

const DeviceRow: FC<{ device: Device; onBlock: (d: Device) => void; busy: boolean }> = ({ device, onBlock, busy }) => {
  const { t } = useTranslation();
  const isPending = !device.last_seen_at;
  const title = [
    device.client_name || (isPending ? t("userDevices.notConnected") : t("userDevices.unknownClient")),
    device.client_version,
  ]
    .filter(Boolean)
    .join(" ");
  const meta = [device.platform, device.os_version, device.device_model, device.locale].filter(Boolean).join(" · ");
  const canBlock = !!(device.hwid || device.device_id || device.device_model);

  return (
    <HStack
      borderWidth="1px"
      borderRadius="md"
      p={3}
      alignItems="flex-start"
      spacing={3}
      _dark={{ borderColor: "gray.600" }}
      opacity={isPending ? 0.75 : 1}
    >
      <Box color="gray.500" _dark={{ color: "gray.400" }} pt="2px" flexShrink={0}>
        {platformIcon(device.platform)}
      </Box>
      <VStack align="stretch" spacing={0.5} flex={1} minW={0}>
        <HStack spacing={2} flexWrap="wrap">
          <Text fontWeight="medium" fontSize="sm">{title}</Text>
          {isPending ? (
            <Badge colorScheme="orange" variant="subtle">{t("userDevices.pending")}</Badge>
          ) : (
            <Hint>{t("userDevices.fetches", { count: device.fetch_count })}</Hint>
          )}
        </HStack>
        {meta && <Hint>{meta}</Hint>}
        {device.hwid && (
          <Hint>
            HWID: <Code fontSize="xs" bg="transparent" px={0}>{device.hwid}</Code>
          </Hint>
        )}
        {device.device_id && (
          <Tooltip label={device.device_id} placement="top-start" hasArrow>
            <Hint w="fit-content">
              ID: <Code fontSize="xs" bg="transparent" px={0}>{device.device_id.slice(0, 12)}…</Code>
            </Hint>
          </Tooltip>
        )}
        {device.last_seen_at && (
          <Tooltip label={formatServerDateTime(device.last_seen_at)} hasArrow placement="top-start">
            <Hint w="fit-content">
              {t("userDevices.lastSeen")}: {relativeFromServer(device.last_seen_at)}
              {device.last_ip ? ` · ${device.last_ip}` : ""}
            </Hint>
          </Tooltip>
        )}
        {device.user_agent && (
          <Tooltip label={device.user_agent} placement="top-start" hasArrow>
            <Hint noOfLines={1} color="gray.400" _dark={{ color: "gray.500" }}>{device.user_agent}</Hint>
          </Tooltip>
        )}
      </VStack>
      {canBlock && (
        <Button
          size="xs"
          colorScheme="red"
          variant="ghost"
          flexShrink={0}
          leftIcon={<NoSymbolIcon width={14} height={14} />}
          isDisabled={busy}
          onClick={() => onBlock(device)}
        >
          {t("userDevices.block")}
        </Button>
      )}
    </HStack>
  );
};

const Group: FC<{ title: string; count: number; children: ReactNode }> = ({ title, count, children }) => (
  <VStack align="stretch" spacing={2}>
    <HStack spacing={2}>
      <Text fontSize="xs" fontWeight="semibold" textTransform="uppercase" letterSpacing="wide" color="gray.600" _dark={{ color: "gray.400" }}>
        {title}
      </Text>
      <Badge variant="subtle">{count}</Badge>
    </HStack>
    {children}
  </VStack>
);

export const UserDevicesModal: FC = () => {
  const { devicesUser } = useDashboard();
  const { t } = useTranslation();
  const toast = useAppToast();
  const { userData } = useGetUser();
  const username = devicesUser?.username;
  const { data, isLoading, isError, refetch, isFetching } = useUserDevices(username);
  const block = useBlockDevice(username);
  const reset = useResetUserDevices(username);
  const [blocking, setBlocking] = useState<Device | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);

  const onClose = () => useDashboard.setState({ devicesUser: null });

  // Sudo only — device rows feed the statistics cards and device billing.
  const canReset = !!devicesUser && !!userData.is_sudo;

  const active = (data ?? []).filter((d) => !!d.last_seen_at);
  const pending = (data ?? []).filter((d) => !d.last_seen_at);

  const doBlock = async () => {
    if (!blocking) return;
    try {
      await block.mutateAsync(blocking.id);
      toast.success(t("userDevices.blockedToast"));
      setBlocking(null);
    } catch (e) {
      toast.error(e, t("userDevices.blockError"));
    }
  };
  const doReset = async () => {
    try {
      const updated = (await reset.mutateAsync()) as User;
      if (updated && updated.username) useDashboard.setState({ devicesUser: updated });
      toast.success(t("userDialog.policy.resetDevicesDone"));
      setConfirmReset(false);
    } catch (e) {
      toast.error(e, t("userDialog.policy.resetDevicesError"));
    }
  };

  const limit = devicesUser?.device_limit ?? null;

  return (
    <ModalShell
      isOpen={!!devicesUser}
      onClose={onClose}
      size="lg"
      scrollBehavior="inside"
      icon={<DevicePhoneMobileIcon width={20} height={20} />}
      title={t("userDevices.title", { username: username || "" })}
      subtitle={
        limit != null
          ? t("userDialog.policy.devicesBadge", { count: active.length, limit })
          : t("userDevices.subtitleUnlimited")
      }
      footer={
        <ModalActions align="end">
          {canReset && (
            <Button
              size="sm"
              variant="outline"
              colorScheme="orange"
              leftIcon={<ShieldExclamationIcon width={14} height={14} />}
              isDisabled={isLoading || (data?.length ?? 0) === 0}
              onClick={() => setConfirmReset(true)}
            >
              {t("userDialog.policy.resetDevices")}
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={() => refetch()} isLoading={isFetching && !isLoading}>
            {t("userDevices.refresh")}
          </Button>
          <Button size="sm" onClick={onClose}>{t("close")}</Button>
        </ModalActions>
      }
    >
      {isError ? (
        <Alert status="error" borderRadius="md" fontSize="sm">
          <AlertIcon />
          {t("userDevices.loadError")}
        </Alert>
      ) : isLoading ? (
        <VStack align="stretch" spacing={2}>
          {[0, 1, 2].map((i) => <Skeleton key={i} h="72px" borderRadius="md" />)}
        </VStack>
      ) : (data?.length ?? 0) === 0 ? (
        <EmptyState compact title={t("userDevices.empty")} />
      ) : (
        <VStack align="stretch" spacing={5}>
          {active.length > 0 && (
            <Group title={t("userDevices.groupActive")} count={active.length}>
              {active.map((d) => <DeviceRow key={d.id} device={d} onBlock={setBlocking} busy={block.isLoading} />)}
            </Group>
          )}
          {pending.length > 0 && (
            <Group title={t("userDevices.groupPending")} count={pending.length}>
              {pending.map((d) => <DeviceRow key={d.id} device={d} onBlock={setBlocking} busy={block.isLoading} />)}
            </Group>
          )}
        </VStack>
      )}

      <ConfirmModal
        isOpen={!!blocking}
        onClose={() => setBlocking(null)}
        onConfirm={doBlock}
        icon={<NoSymbolIcon width={20} height={20} />}
        title={t("userDevices.blockConfirmTitle")}
        prompt={t("userDevices.blockConfirmBody")}
        confirmLabel={t("userDevices.blockConfirm")}
      >
        {blocking && !blocking.hwid && !blocking.device_id && blocking.device_model && (
          <Text fontSize="xs" color="orange.500">
            {t("userDevices.blockModelWarning", { model: blocking.device_model })}
          </Text>
        )}
      </ConfirmModal>
      <ConfirmModal
        isOpen={confirmReset}
        onClose={() => setConfirmReset(false)}
        onConfirm={doReset}
        icon={<ShieldExclamationIcon width={20} height={20} />}
        colorScheme="orange"
        title={t("userDialog.policy.resetDevices")}
        prompt={t("userDialog.policy.resetDevicesConfirm", { username: username ?? "" })}
        confirmLabel={t("userDialog.policy.resetDevices")}
      />
    </ModalShell>
  );
};
