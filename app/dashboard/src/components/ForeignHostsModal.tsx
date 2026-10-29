import {
  Alert,
  AlertIcon,
  Badge,
  Box,
  Button,
  Code,
  HStack,
  IconButton,
  Input,
  Progress,
  Skeleton,
  Switch,
  Tab,
  Table,
  TabList,
  TabPanel,
  TabPanels,
  Tabs,
  Tbody,
  Td,
  Text,
  Th,
  Thead,
  Tooltip,
  Tr,
  useBreakpointValue,
  VStack,
  Wrap,
  WrapItem,
} from "@chakra-ui/react";
import {
  GlobeAltIcon,
  LinkIcon,
  PencilSquareIcon,
  PlusIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import { zodResolver } from "@hookform/resolvers/zod";
import {
  AdminChips,
  AdminPicker,
  ConfirmModal,
  EmptyState,
  FormField,
  Hint,
  ModalActions,
  ModalShell,
} from "components/shared";
import { useAppToast } from "hooks/useAppToast";
import {
  ExternalSubscription,
  parseRemoteUserinfo,
  useAddExternalSubscription,
  useDeleteExternalSubscription,
  useExternalSubscriptions,
  usePollerIdentity,
  useUpdateExternalSubscription,
} from "hooks/useExternalSubscriptions";
import {
  ManualHost,
  useAddManualHost,
  useDeleteManualHost,
  useManualHosts,
  useUpdateManualHost,
} from "hooks/useManualHosts";
import { FC, ReactNode, useEffect, useMemo, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { relativeFromServer } from "utils/dates";
import { formatBytes } from "utils/formatByte";
import { z } from "zod";

// ---------------------------------------------------------------------------
// Shared list chrome: a themed table on md+, stacked cards on phones.
// ---------------------------------------------------------------------------
type Column = { key: string; label: ReactNode; isNumeric?: boolean; w?: string; /** On phone cards: full width, no label. */ primary?: boolean };
type RowCells = { key: string | number; cells: Record<string, ReactNode>; onClick?: () => void };

const ResponsiveList: FC<{ columns: Column[]; rows: RowCells[] }> = ({ columns, rows }) => {
  const isTable = useBreakpointValue({ base: false, md: true }, { ssr: false });
  if (isTable === false) {
    return (
      <VStack align="stretch" spacing={3}>
        {rows.map((r) => (
          <Box key={r.key} borderWidth="1px" borderRadius="md" p={3} _dark={{ borderColor: "gray.600" }}>
            <VStack align="stretch" spacing={2}>
              {columns.map((c) =>
                r.cells[c.key] == null ? null : c.primary || !c.label ? (
                  <Box key={c.key} minW={0}>
                    {r.cells[c.key]}
                  </Box>
                ) : (
                  <HStack key={c.key} justifyContent="space-between" alignItems="flex-start" spacing={3}>
                    <Hint flexShrink={0}>{c.label}</Hint>
                    <Box flex={1} minW={0} textAlign="right">
                      {r.cells[c.key]}
                    </Box>
                  </HStack>
                )
              )}
            </VStack>
          </Box>
        ))}
      </VStack>
    );
  }
  return (
    <Box overflowX="auto">
      <Table size="sm">
        <Thead>
          <Tr>
            {columns.map((c) => (
              <Th key={c.key} isNumeric={c.isNumeric} w={c.w}>
                {c.label}
              </Th>
            ))}
          </Tr>
        </Thead>
        <Tbody>
          {rows.map((r) => (
            <Tr key={r.key}>
              {columns.map((c) => (
                <Td key={c.key} isNumeric={c.isNumeric} verticalAlign="top" w={c.w} whiteSpace={c.key === "admins" ? "nowrap" : undefined}>
                  {r.cells[c.key]}
                </Td>
              ))}
            </Tr>
          ))}
        </Tbody>
      </Table>
    </Box>
  );
};

const RowActions: FC<{
  enabled: boolean;
  busy: boolean;
  onToggle: (v: boolean) => void;
  onEdit: () => void;
  onDelete: () => void;
}> = ({ enabled, busy, onToggle, onEdit, onDelete }) => {
  const { t } = useTranslation();
  return (
    <HStack spacing={1} justifyContent="flex-end">
      <Tooltip label={enabled ? t("foreignHosts.enabled") : t("foreignHosts.disabled")} hasArrow>
        <Box>
          <Switch
            size="sm"
            colorScheme="primary"
            isChecked={enabled}
            isDisabled={busy}
            onChange={(e) => onToggle(e.target.checked)}
            aria-label={t("foreignHosts.enabled")}
          />
        </Box>
      </Tooltip>
      <IconButton
        aria-label={t("foreignHosts.edit")}
        size="xs"
        variant="ghost"
        icon={<PencilSquareIcon width={16} height={16} />}
        isDisabled={busy}
        onClick={onEdit}
      />
      <IconButton
        aria-label={t("delete")}
        size="xs"
        variant="ghost"
        colorScheme="red"
        icon={<TrashIcon width={16} height={16} />}
        isDisabled={busy}
        onClick={onDelete}
      />
    </HStack>
  );
};

const ListSkeleton: FC = () => (
  <VStack align="stretch" spacing={2}>
    {[0, 1, 2].map((i) => (
      <Skeleton key={i} h="44px" borderRadius="md" />
    ))}
  </VStack>
);

// ---------------------------------------------------------------------------
// External subscriptions
// ---------------------------------------------------------------------------
const externalSchema = z.object({
  name: z.string().trim().min(1, "foreignHosts.errors.nameRequired").max(64, "foreignHosts.errors.nameLong"),
  url: z.string().trim().refine((v) => /^https?:\/\/\S+$/.test(v), "foreignHosts.errors.url"),
  hwid: z.string().trim(),
  admin_ids: z.array(z.number()),
});
type ExternalForm = z.infer<typeof externalSchema>;

const ExternalDialog: FC<{
  isOpen: boolean;
  onClose: () => void;
  editing: ExternalSubscription | null;
}> = ({ isOpen, onClose, editing }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const add = useAddExternalSubscription();
  const update = useUpdateExternalSubscription();
  const identity = usePollerIdentity(isOpen);
  const defaults = useMemo<ExternalForm>(
    () => ({
      name: editing?.name ?? "",
      url: editing?.url ?? "",
      hwid: editing?.hwid ?? "",
      admin_ids: editing?.admin_ids ?? [],
    }),
    [editing?.id, isOpen]
  );
  const form = useForm<ExternalForm>({ resolver: zodResolver(externalSchema), defaultValues: defaults });
  useEffect(() => form.reset(defaults), [defaults]);
  const errors = form.formState.errors;
  const busy = add.isLoading || update.isLoading;

  const submit = async (v: ExternalForm) => {
    try {
      if (editing) {
        const body: Record<string, unknown> = {};
        if (v.name !== editing.name) body.name = v.name;
        if (v.url !== editing.url) body.url = v.url;
        const h = v.hwid || null;
        if (h !== (editing.hwid ?? null)) body.hwid = h;
        const before = [...editing.admin_ids].sort().join(",");
        const after = [...v.admin_ids].sort().join(",");
        if (before !== after) body.admin_ids = v.admin_ids;
        if (Object.keys(body).length) await update.mutateAsync({ id: editing.id, body });
        toast.success(t("externalSubs.updated"));
      } else {
        await add.mutateAsync({ name: v.name, url: v.url, hwid: v.hwid || null, admin_ids: v.admin_ids });
        toast.success(t("externalSubs.added"));
      }
      onClose();
    } catch (e) {
      toast.error(e, t("externalSubs.error"));
    }
  };

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      size="lg"
      scrollBehavior="inside"
      icon={<GlobeAltIcon width={20} height={20} />}
      title={editing ? t("foreignHosts.external.editTitle") : t("foreignHosts.external.addTitle")}
      subtitle={editing ? t("externalSubs.editHelp") : undefined}
      isBusy={busy}
      footer={
        <ModalActions>
          <Button size="sm" variant="outline" onClick={onClose} isDisabled={busy}>
            {t("cancel")}
          </Button>
          <Button size="sm" colorScheme="primary" isLoading={busy} onClick={form.handleSubmit(submit)}>
            {editing ? t("announcements.save") : t("externalSubs.add")}
          </Button>
        </ModalActions>
      }
    >
      <form onSubmit={form.handleSubmit(submit)}>
        <VStack align="stretch" spacing={4}>
          <FormField label={t("foreignHosts.name")} error={errors.name?.message && t(errors.name.message)} isRequired>
            <Input placeholder={t("externalSubs.namePlaceholder")} {...form.register("name")} />
          </FormField>
          <FormField
            label={t("foreignHosts.external.url")}
            helper={t("foreignHosts.external.urlHelp")}
            error={errors.url?.message && t(errors.url.message)}
            isRequired
          >
            <Input placeholder="https://panel.example/sub/<token>" {...form.register("url")} />
          </FormField>
          <FormField
            label={t("foreignHosts.external.hwid")}
            helper={
              <>
                {t("externalSubs.hwidHelp")}{" "}
                {identity.data?.hwid ? <Code fontSize="xs">{identity.data.hwid}</Code> : null}
              </>
            }
            error={errors.hwid?.message && t(errors.hwid.message)}
          >
            <Input placeholder={t("externalSubs.hwidPlaceholder")} {...form.register("hwid")} />
          </FormField>
          <Controller
            control={form.control}
            name="admin_ids"
            render={({ field }) => (
              <AdminPicker value={field.value} onChange={field.onChange} emptyMeans="nobody" helper={t("foreignHosts.assignHelp")} />
            )}
          />
        </VStack>
      </form>
    </ModalShell>
  );
};

const ExternalStatus: FC<{ sub: ExternalSubscription }> = ({ sub }) => {
  const { t } = useTranslation();
  const status = sub.status || {};
  const info = parseRemoteUserinfo(status.remote_userinfo);
  const hosts = status.host_counts?.v2ray ?? 0;
  const state = status.ok === true ? "ok" : status.ok === false ? "failed" : "pending";
  return (
    <VStack align="flex-start" spacing={1}>
      <Wrap spacing={1} align="center">
        <WrapItem>
          {state === "ok" && <Badge colorScheme="green">{t("externalSubs.hosts", { count: hosts })}</Badge>}
          {state === "failed" && (
            <Tooltip label={status.error ?? ""} hasArrow isDisabled={!status.error}>
              <Badge colorScheme="red">{t("externalSubs.failed")}</Badge>
            </Tooltip>
          )}
          {state === "pending" && <Badge>{t("externalSubs.pending")}</Badge>}
        </WrapItem>
        {(status.countries ?? []).map((cc) => (
          <WrapItem key={cc}>
            <Badge variant="outline" fontSize="xs">{cc}</Badge>
          </WrapItem>
        ))}
      </Wrap>
      {status.error && (
        // ok=true with an error means the essential format worked but a
        // secondary one (clash / sing-box) failed — a warning, not a failure.
        <Text
          fontSize="xs"
          color={state === "failed" ? "red.500" : "orange.500"}
          noOfLines={2}
          title={status.error}
        >
          {status.http_status ? `HTTP ${status.http_status}: ` : ""}
          {status.error}
        </Text>
      )}
      {info && (
        <Box w="full" minW="200px">
          <Hint whiteSpace="nowrap">
            {formatBytes(info.used)}
            {info.total > 0 ? ` / ${formatBytes(info.total)}` : " / ∞"}
          </Hint>
          <Hint whiteSpace="nowrap" color={info.daysLeft !== null && info.daysLeft < 7 ? "red.500" : undefined}>
            {info.expire
              ? t("externalSubs.expires", { date: info.expire.toLocaleDateString(), days: info.daysLeft })
              : t("externalSubs.neverExpires")}
          </Hint>
          {info.percentUsed !== null && (
            <Progress
              size="xs"
              mt={1}
              value={info.percentUsed}
              colorScheme={info.percentUsed > 90 ? "red" : "primary"}
              borderRadius="sm"
            />
          )}
        </Box>
      )}
      {status.fetched_at && (
        <Hint>{t("externalSubs.lastPoll", { when: relativeFromServer(status.fetched_at) })}</Hint>
      )}
    </VStack>
  );
};

const ExternalTab: FC<{ active: boolean }> = ({ active }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const list = useExternalSubscriptions(active);
  const update = useUpdateExternalSubscription();
  const remove = useDeleteExternalSubscription();
  const [dialog, setDialog] = useState<{ open: boolean; editing: ExternalSubscription | null }>({ open: false, editing: null });
  const [deleting, setDeleting] = useState<ExternalSubscription | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const toggle = (sub: ExternalSubscription, enabled: boolean) => {
    setBusyId(sub.id);
    update.mutate(
      { id: sub.id, body: { enabled } },
      {
        onError: (e) => toast.error(e, t("externalSubs.error")),
        onSettled: () => setBusyId(null),
      }
    );
  };
  const confirmDelete = async () => {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.id);
      toast.success(t("foreignHosts.deleted", { name: deleting.name }));
      setDeleting(null);
    } catch (e) {
      toast.error(e, t("externalSubs.error"));
    }
  };

  const columns: Column[] = [
    { key: "name", label: t("foreignHosts.name"), primary: true, w: "30%" },
    { key: "status", label: t("foreignHosts.status"), w: "44%" },
    { key: "admins", label: t("adminPicker.label"), w: "18%" },
    { key: "actions", label: "", w: "1%" },
  ];
  const rows: RowCells[] = (list.data ?? []).map((sub) => ({
    key: sub.id,
    cells: {
      name: (
        <VStack align="flex-start" spacing={0} minW={0}>
          <HStack spacing={2}>
            <Text fontWeight="medium" fontSize="sm">{sub.name}</Text>
            {!sub.enabled && <Badge colorScheme="gray">{t("foreignHosts.disabled")}</Badge>}
          </HStack>
          <Hint noOfLines={1} title={sub.url} maxW="320px">{sub.url}</Hint>
          {!sub.hwid && (
            <Tooltip label={t("externalSubs.fallbackHwidWarning")} hasArrow>
              <Text fontSize="xs" color="orange.500">{t("externalSubs.fallbackHwid")}</Text>
            </Tooltip>
          )}
        </VStack>
      ),
      admins: <AdminChips ids={sub.admin_ids ?? []} emptyMeans="nobody" />,
      status: <ExternalStatus sub={sub} />,
      actions: (
        <RowActions
          enabled={sub.enabled}
          busy={busyId === sub.id}
          onToggle={(v) => toggle(sub, v)}
          onEdit={() => setDialog({ open: true, editing: sub })}
          onDelete={() => setDeleting(sub)}
        />
      ),
    },
  }));

  return (
    <VStack align="stretch" spacing={4}>
      <HStack justifyContent="space-between" alignItems="flex-start">
        <Hint size="sm">{t("externalSubs.help")}</Hint>
        <Button
          size="sm"
          colorScheme="primary"
          flexShrink={0}
          leftIcon={<PlusIcon width={14} height={14} />}
          onClick={() => setDialog({ open: true, editing: null })}
        >
          {t("externalSubs.add")}
        </Button>
      </HStack>
      {list.isError ? (
        <Alert status="error" borderRadius="md" fontSize="sm"><AlertIcon />{t("foreignHosts.loadError")}</Alert>
      ) : list.isLoading ? (
        <ListSkeleton />
      ) : rows.length === 0 ? (
        <EmptyState
          compact
          title={t("externalSubs.empty")}
          actionLabel={t("externalSubs.add")}
          onAction={() => setDialog({ open: true, editing: null })}
        />
      ) : (
        <ResponsiveList columns={columns} rows={rows} />
      )}
      <ExternalDialog
        isOpen={dialog.open}
        onClose={() => setDialog({ open: false, editing: null })}
        editing={dialog.editing}
      />
      <ConfirmModal
        isOpen={!!deleting}
        onClose={() => setDeleting(null)}
        onConfirm={confirmDelete}
        icon={<TrashIcon width={20} height={20} />}
        title={t("foreignHosts.deleteTitle")}
        prompt={t("foreignHosts.external.deletePrompt", { name: deleting?.name ?? "" })}
        confirmLabel={t("delete")}
      />
    </VStack>
  );
};

// ---------------------------------------------------------------------------
// Manual hosts
// ---------------------------------------------------------------------------
const manualSchema = z.object({
  name: z.string().trim().min(1, "foreignHosts.errors.nameRequired").max(64, "foreignHosts.errors.nameLong"),
  link: z
    .string()
    .trim()
    .refine((v) => /^(vless|vmess|trojan|ss):\/\/\S+$/.test(v), "foreignHosts.errors.link")
    .refine((v) => !/[\r\n]/.test(v), "foreignHosts.errors.singleLine"),
  admin_ids: z.array(z.number()),
});
type ManualForm = z.infer<typeof manualSchema>;

const ManualDialog: FC<{ isOpen: boolean; onClose: () => void; editing: ManualHost | null }> = ({
  isOpen,
  onClose,
  editing,
}) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const add = useAddManualHost();
  const update = useUpdateManualHost();
  const defaults = useMemo<ManualForm>(
    () => ({ name: editing?.name ?? "", link: editing?.link ?? "", admin_ids: editing?.admin_ids ?? [] }),
    [editing?.id, isOpen]
  );
  const form = useForm<ManualForm>({ resolver: zodResolver(manualSchema), defaultValues: defaults });
  useEffect(() => form.reset(defaults), [defaults]);
  const errors = form.formState.errors;
  const busy = add.isLoading || update.isLoading;

  const submit = async (v: ManualForm) => {
    try {
      if (editing) {
        const body: Partial<Pick<ManualHost, "name" | "link" | "admin_ids">> = {};
        if (v.name !== editing.name) body.name = v.name;
        if (v.link !== editing.link) body.link = v.link;
        const before = [...editing.admin_ids].sort().join(",");
        const after = [...v.admin_ids].sort().join(",");
        if (before !== after) body.admin_ids = v.admin_ids;
        if (Object.keys(body).length) await update.mutateAsync({ id: editing.id, body });
        toast.success(t("manualHosts.updated"));
      } else {
        await add.mutateAsync({ name: v.name, link: v.link, admin_ids: v.admin_ids });
        toast.success(t("manualHosts.added"));
      }
      onClose();
    } catch (e) {
      toast.error(e, t("manualHosts.error"));
    }
  };

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      size="lg"
      scrollBehavior="inside"
      icon={<LinkIcon width={20} height={20} />}
      title={editing ? t("foreignHosts.manual.editTitle") : t("foreignHosts.manual.addTitle")}
      isBusy={busy}
      footer={
        <ModalActions>
          <Button size="sm" variant="outline" onClick={onClose} isDisabled={busy}>
            {t("cancel")}
          </Button>
          <Button size="sm" colorScheme="primary" isLoading={busy} onClick={form.handleSubmit(submit)}>
            {editing ? t("announcements.save") : t("manualHosts.add")}
          </Button>
        </ModalActions>
      }
    >
      <form onSubmit={form.handleSubmit(submit)}>
        <VStack align="stretch" spacing={4}>
          <FormField label={t("foreignHosts.name")} error={errors.name?.message && t(errors.name.message)} isRequired>
            <Input placeholder={t("manualHosts.namePlaceholder")} {...form.register("name")} />
          </FormField>
          <FormField
            label={t("foreignHosts.manual.link")}
            helper={t("foreignHosts.manual.linkHelp")}
            error={errors.link?.message && t(errors.link.message)}
            isRequired
          >
            <Input fontFamily="mono" fontSize="sm" placeholder="vless://…" {...form.register("link")} />
          </FormField>
          <Controller
            control={form.control}
            name="admin_ids"
            render={({ field }) => (
              <AdminPicker value={field.value} onChange={field.onChange} emptyMeans="nobody" helper={t("foreignHosts.assignHelp")} />
            )}
          />
        </VStack>
      </form>
    </ModalShell>
  );
};

const ManualTab: FC<{ active: boolean }> = ({ active }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const list = useManualHosts(active);
  const update = useUpdateManualHost();
  const remove = useDeleteManualHost();
  const [dialog, setDialog] = useState<{ open: boolean; editing: ManualHost | null }>({ open: false, editing: null });
  const [deleting, setDeleting] = useState<ManualHost | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const toggle = (host: ManualHost, enabled: boolean) => {
    setBusyId(host.id);
    update.mutate(
      { id: host.id, body: { enabled } },
      { onError: (e) => toast.error(e, t("manualHosts.error")), onSettled: () => setBusyId(null) }
    );
  };
  const confirmDelete = async () => {
    if (!deleting) return;
    try {
      await remove.mutateAsync(deleting.id);
      toast.success(t("foreignHosts.deleted", { name: deleting.name }));
      setDeleting(null);
    } catch (e) {
      toast.error(e, t("manualHosts.error"));
    }
  };

  const columns: Column[] = [
    { key: "name", label: t("foreignHosts.name"), primary: true, w: "62%" },
    { key: "admins", label: t("adminPicker.label"), w: "30%" },
    { key: "actions", label: "", w: "1%" },
  ];
  const rows: RowCells[] = (list.data ?? []).map((host) => ({
    key: host.id,
    cells: {
      name: (
        <VStack align="flex-start" spacing={0} minW={0}>
          <HStack spacing={2}>
            <Text fontWeight="medium" fontSize="sm">{host.name}</Text>
            {!host.enabled && <Badge colorScheme="gray">{t("foreignHosts.disabled")}</Badge>}
          </HStack>
          <Code fontSize="xs" noOfLines={1} maxW="320px" title={host.link} bg="transparent" px={0}>
            {host.link}
          </Code>
        </VStack>
      ),
      admins: <AdminChips ids={host.admin_ids} emptyMeans="nobody" />,
      actions: (
        <RowActions
          enabled={host.enabled}
          busy={busyId === host.id}
          onToggle={(v) => toggle(host, v)}
          onEdit={() => setDialog({ open: true, editing: host })}
          onDelete={() => setDeleting(host)}
        />
      ),
    },
  }));

  return (
    <VStack align="stretch" spacing={4}>
      <HStack justifyContent="space-between" alignItems="flex-start">
        <Hint size="sm">{t("manualHosts.help")}</Hint>
        <Button
          size="sm"
          colorScheme="primary"
          flexShrink={0}
          leftIcon={<PlusIcon width={14} height={14} />}
          onClick={() => setDialog({ open: true, editing: null })}
        >
          {t("manualHosts.add")}
        </Button>
      </HStack>
      {list.isError ? (
        <Alert status="error" borderRadius="md" fontSize="sm"><AlertIcon />{t("foreignHosts.loadError")}</Alert>
      ) : list.isLoading ? (
        <ListSkeleton />
      ) : rows.length === 0 ? (
        <EmptyState
          compact
          title={t("manualHosts.empty")}
          actionLabel={t("manualHosts.add")}
          onAction={() => setDialog({ open: true, editing: null })}
        />
      ) : (
        <ResponsiveList columns={columns} rows={rows} />
      )}
      <ManualDialog
        isOpen={dialog.open}
        onClose={() => setDialog({ open: false, editing: null })}
        editing={dialog.editing}
      />
      <ConfirmModal
        isOpen={!!deleting}
        onClose={() => setDeleting(null)}
        onConfirm={confirmDelete}
        icon={<TrashIcon width={20} height={20} />}
        title={t("foreignHosts.deleteTitle")}
        prompt={t("manualHosts.deleteConfirm", { name: deleting?.name ?? "" })}
        confirmLabel={t("delete")}
      />
    </VStack>
  );
};

// ---------------------------------------------------------------------------
type Props = { isOpen: boolean; onClose: () => void };

/** Hosts that live elsewhere: subscriptions mirrored from other panels of
 * this family (polled by the Rust sidecar) and share links pasted by hand.
 * One screen, two tabs, the same list/dialog/confirm chrome as the rest of
 * the panel. Sudo-only on the backend. */
export const ForeignHostsModal: FC<Props> = ({ isOpen, onClose }) => {
  const { t } = useTranslation();
  const [tab, setTab] = useState(0);
  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      size="3xl"
      scrollBehavior="inside"
      icon={<GlobeAltIcon width={20} height={20} />}
      title={t("foreignHosts.title")}
      subtitle={t("foreignHosts.subtitle")}
    >
      <Tabs variant="soft-rounded" colorScheme="primary" size="sm" index={tab} onChange={setTab} isLazy>
        <TabList mb={4}>
          <Tab>{t("externalSubs.title")}</Tab>
          <Tab>{t("manualHosts.title")}</Tab>
        </TabList>
        <TabPanels>
          <TabPanel px={0} pt={0}><ExternalTab active={isOpen && tab === 0} /></TabPanel>
          <TabPanel px={0} pt={0}><ManualTab active={isOpen && tab === 1} /></TabPanel>
        </TabPanels>
      </Tabs>
    </ModalShell>
  );
};
