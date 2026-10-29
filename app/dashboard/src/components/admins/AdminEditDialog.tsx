import {
  Button,
  HStack,
  IconButton,
  Input,
  Switch,
  Tab,
  TabList,
  TabPanel,
  TabPanels,
  Tabs,
  VStack,
} from "@chakra-ui/react";
import {
  ArrowPathIcon,
  ChartBarIcon,
  PlusIcon,
  ShieldCheckIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import { zodResolver } from "@hookform/resolvers/zod";
import {
  ConfirmModal,
  FormField,
  Hint,
  ModalActions,
  ModalShell,
} from "components/shared";
import { AdminListItem } from "hooks/useAdmins";
import {
  AdminPatch,
  UsageAdjustItem,
  useAdjustAdminUsage,
  useUpdateAdmin,
} from "hooks/useAdminManagement";
import { useAppToast } from "hooks/useAppToast";
import { useRegenerateSubs } from "hooks/useRegenerateSubs";
import { FC, useEffect, useMemo, useState } from "react";
import { useFieldArray, useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { ashgabatToday } from "utils/dates";
import { formatBytes } from "utils/formatByte";
import { z } from "zod";
import { codePoints, isSubscriptionUrl, PASSWORD_MIN } from "./rules";
import { FLAGS } from "./traits";

// ---------------------------------------------------------------------------
// Settings tab: one labelled switch per flag, each PATCHing only its own field.
// ---------------------------------------------------------------------------
const SettingsPanel: FC<{ admin: AdminListItem }> = ({ admin }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const update = useUpdateAdmin();
  return (
    <VStack align="stretch" spacing={4}>
      {FLAGS.map((f) => (
        <FormField
          key={f.key}
          label={t(f.label)}
          helper={t(f.help)}
          labelAddon={
            <Switch
              size="sm"
              colorScheme="primary"
              isChecked={!!admin[f.key]}
              isDisabled={update.isLoading}
              aria-label={t(f.label)}
              onChange={(e) =>
                update.mutate(
                  { username: admin.username, body: { [f.key]: e.target.checked } as AdminPatch },
                  { onError: (err) => toast.error(err, t("announcements.error")) }
                )
              }
            />
          }
        />
      ))}
    </VStack>
  );
};

// ---------------------------------------------------------------------------
// Credentials tab: react-hook-form + zod, empty = unchanged.
// ---------------------------------------------------------------------------
const credentialsSchema = z.object({
  password: z.string().refine((v) => !v || codePoints(v) >= PASSWORD_MIN, "admins.errors.passwordMin"),
  telegram_id: z.string().regex(/^\d*$/, "admins.errors.telegramDigits"),
  discord_webhook: z
    .string()
    .refine((v) => !v || v.startsWith("https://discord.com"), "admins.errors.discordPrefix"),
  subscription_url: z
    .string()
    .refine((v) => !v.trim() || isSubscriptionUrl(v.trim()), "admins.errors.url"),
});
type CredentialsForm = z.infer<typeof credentialsSchema>;

const CredentialsPanel: FC<{ admin: AdminListItem }> = ({ admin }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const update = useUpdateAdmin();
  const defaults = useMemo<CredentialsForm>(
    () => ({
      password: "",
      telegram_id: admin.telegram_id != null ? String(admin.telegram_id) : "",
      discord_webhook: admin.discord_webhook ?? "",
      subscription_url: admin.subscription_url ?? "",
    }),
    [admin.username, admin.telegram_id, admin.discord_webhook, admin.subscription_url]
  );
  const form = useForm<CredentialsForm>({
    resolver: zodResolver(credentialsSchema),
    defaultValues: defaults,
  });
  useEffect(() => form.reset(defaults), [defaults]);
  const errors = form.formState.errors;

  const submit = async (values: CredentialsForm) => {
    const body: AdminPatch = {};
    if (values.password) body.password = values.password;
    if (values.telegram_id && values.telegram_id !== defaults.telegram_id)
      body.telegram_id = Number(values.telegram_id);
    if (values.discord_webhook && values.discord_webhook !== defaults.discord_webhook)
      body.discord_webhook = values.discord_webhook.trim();
    // Trimmed first: the backend reads "" as "clear", which would move every
    // link of this admin to the panel default domain. Blank = unchanged.
    const url = values.subscription_url.trim();
    if (url && url !== defaults.subscription_url) body.subscription_url = url;
    if (!Object.keys(body).length) {
      toast.info(t("announcements.nothingToSave"));
      return;
    }
    try {
      await update.mutateAsync({ username: admin.username, body });
      toast.success(
        t(body.password ? "announcements.adminSavedSignedOut" : "announcements.adminSaved")
      );
      form.setValue("password", "");
    } catch (e) {
      toast.error(e, t("announcements.error"));
    }
  };

  return (
    <form onSubmit={form.handleSubmit(submit)}>
      <VStack align="stretch" spacing={4}>
        <Hint size="sm">{t("announcements.adminFieldsHelp")}</Hint>
        <FormField label={t("admins.newPassword")} error={errors.password?.message && t(errors.password.message)}>
          <Input type="password" autoComplete="new-password" placeholder={t("admins.newPasswordPlaceholder")} {...form.register("password")} />
        </FormField>
        <FormField label={t("announcements.telegramId")} error={errors.telegram_id?.message && t(errors.telegram_id.message)}>
          <Input inputMode="numeric" placeholder="123456789" {...form.register("telegram_id")} />
        </FormField>
        <FormField label={t("announcements.discordWebhook")} error={errors.discord_webhook?.message && t(errors.discord_webhook.message)}>
          <Input placeholder="https://discord.com/api/webhooks/…" {...form.register("discord_webhook")} />
        </FormField>
        <FormField
          label={t("announcements.subscriptionUrl")}
          helper={t("admins.subscriptionUrlHelp")}
          error={errors.subscription_url?.message && t(errors.subscription_url.message)}
        >
          <Input placeholder="https://sub.example.com" {...form.register("subscription_url")} />
        </FormField>
        <ModalActions align="end">
          <Button size="sm" type="submit" colorScheme="primary" isLoading={update.isLoading}>
            {t("announcements.save")}
          </Button>
        </ModalActions>
      </VStack>
    </form>
  );
};

// ---------------------------------------------------------------------------
// Traffic tab: repeater of (day, GB) rows, validated, confirmed before apply.
// ---------------------------------------------------------------------------
const trafficSchema = z
  .object({
    rows: z
      .array(
        z.object({
          date: z.string().min(1, "admins.errors.dateRequired"),
          gb: z.coerce
            .number({ invalid_type_error: "admins.errors.gbPositive" })
            .gt(0, "admins.errors.gbPositive")
            .max(100000, "admins.errors.gbMax"),
        })
      )
      .min(1),
  })
  .superRefine((v, ctx) => {
    const yesterday = ashgabatToday().subtract(1, "day").format("YYYY-MM-DD");
    const seen = new Set<string>();
    v.rows.forEach((r, i) => {
      if (r.date > yesterday)
        ctx.addIssue({ code: "custom", path: ["rows", i, "date"], message: "admins.errors.dateFuture" });
      if (seen.has(r.date))
        ctx.addIssue({ code: "custom", path: ["rows", i, "date"], message: "admins.errors.duplicateDate" });
      seen.add(r.date);
    });
  });
type TrafficForm = z.infer<typeof trafficSchema>;

const TrafficPanel: FC<{ admin: AdminListItem }> = ({ admin }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const adjust = useAdjustAdminUsage();
  const yesterday = ashgabatToday().subtract(1, "day").format("YYYY-MM-DD");
  const form = useForm<TrafficForm>({
    resolver: zodResolver(trafficSchema),
    defaultValues: { rows: [{ date: yesterday, gb: 0 }] },
  });
  const rows = useFieldArray({ control: form.control, name: "rows" });
  const [pending, setPending] = useState<UsageAdjustItem[] | null>(null);
  const errors = form.formState.errors;

  const apply = async () => {
    if (!pending) return;
    try {
      const res = await adjust.mutateAsync({ username: admin.username, adjustments: pending });
      toast.success(t("announcements.usageSaved", { gb: formatBytes(res.total_added_bytes) }));
      setPending(null);
      form.reset({ rows: [{ date: yesterday, gb: 0 }] });
    } catch (e) {
      toast.error(e, t("announcements.error"));
    }
  };
  const total = pending?.reduce((s, r) => s + r.gb, 0) ?? 0;

  return (
    <form onSubmit={form.handleSubmit((v) => setPending(v.rows.map((r) => ({ date: r.date, gb: Number(r.gb) }))))}>
      <VStack align="stretch" spacing={4}>
        <Hint size="sm">{t("announcements.addTrafficHelp")}</Hint>
        <VStack align="stretch" spacing={3}>
          {rows.fields.map((field, i) => (
            <HStack key={field.id} alignItems="flex-start" spacing={2}>
              <FormField
                label={i === 0 ? t("admins.traffic.day") : undefined}
                error={errors.rows?.[i]?.date?.message && t(errors.rows[i]!.date!.message!)}
                flex={1}
              >
                <Input type="date" size="sm" max={yesterday} {...form.register(`rows.${i}.date` as const)} />
              </FormField>
              <FormField
                label={i === 0 ? t("admins.traffic.amount") : undefined}
                error={errors.rows?.[i]?.gb?.message && t(errors.rows[i]!.gb!.message!)}
                w="140px"
              >
                <Input type="number" size="sm" step="any" min={0} placeholder="0" {...form.register(`rows.${i}.gb` as const)} />
              </FormField>
              <IconButton
                aria-label={t("announcements.trafficRemoveDay")}
                size="sm"
                variant="ghost"
                mt={i === 0 ? "26px" : 0}
                icon={<TrashIcon width={16} height={16} />}
                isDisabled={rows.fields.length <= 1}
                onClick={() => rows.remove(i)}
              />
            </HStack>
          ))}
        </VStack>
        <ModalActions align="end">
          <Button
            size="sm"
            variant="outline"
            leftIcon={<PlusIcon width={14} height={14} />}
            onClick={() => rows.append({ date: yesterday, gb: 0 })}
          >
            {t("announcements.trafficAddDay")}
          </Button>
          <Button size="sm" type="submit" colorScheme="primary">
            {t("announcements.addTraffic")}
          </Button>
        </ModalActions>
      </VStack>
      <ConfirmModal
        isOpen={pending !== null}
        onClose={() => setPending(null)}
        onConfirm={apply}
        icon={<ChartBarIcon width={20} height={20} />}
        colorScheme="orange"
        title={t("announcements.addTraffic")}
        prompt={t("announcements.usageConfirm", {
          username: admin.username,
          gb: total,
          days: pending?.length ?? 0,
        })}
        confirmLabel={t("announcements.addTraffic")}
      />
    </form>
  );
};

// ---------------------------------------------------------------------------
// Links tab: regenerate this admin's Happ links (background job).
// ---------------------------------------------------------------------------
const LinksPanel: FC<{ admin: AdminListItem }> = ({ admin }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const regen = useRegenerateSubs();
  const [confirm, setConfirm] = useState(false);
  const run = async () => {
    try {
      const res = await regen.mutateAsync({ admin_username: admin.username });
      toast.success(t("regen.started", { count: res?.queued ?? 0 }));
      setConfirm(false);
    } catch (e) {
      toast.error(e, t("regen.error"));
    }
  };
  return (
    <VStack align="stretch" spacing={4}>
      <Hint size="sm">{t("regen.adminHelp")}</Hint>
      <Hint>{t("admins.links.domain", { url: admin.subscription_url || t("admins.links.defaultDomain") })}</Hint>
      <ModalActions align="end">
        <Button
          size="sm"
          variant="outline"
          colorScheme="orange"
          leftIcon={<ArrowPathIcon width={14} height={14} />}
          onClick={() => setConfirm(true)}
        >
          {t("regen.admin")}
        </Button>
      </ModalActions>
      <ConfirmModal
        isOpen={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={run}
        icon={<ArrowPathIcon width={20} height={20} />}
        colorScheme="orange"
        title={t("regen.admin")}
        prompt={t("regen.confirmAdmin", { username: admin.username })}
        confirmLabel={t("regen.admin")}
      />
    </VStack>
  );
};

/** The four-tab settings dialog the old Admins modal opened per admin. */
export const AdminEditDialog: FC<{ admin: AdminListItem | null; onClose: () => void }> = ({ admin, onClose }) => {
  const { t } = useTranslation();
  return (
    <ModalShell
      isOpen={!!admin}
      onClose={onClose}
      size="lg"
      scrollBehavior="inside"
      icon={<ShieldCheckIcon width={20} height={20} />}
      title={admin?.username ?? ""}
      subtitle={t("admins.editSubtitle")}
    >
      {admin && (
        <Tabs variant="soft-rounded" colorScheme="primary" size="sm" isLazy>
          <TabList mb={4} flexWrap="wrap">
            <Tab>{t("admins.tab.settings")}</Tab>
            <Tab>{t("admins.tab.credentials")}</Tab>
            <Tab>{t("admins.tab.traffic")}</Tab>
            <Tab>{t("admins.tab.links")}</Tab>
          </TabList>
          <TabPanels>
            <TabPanel px={0} pt={0}><SettingsPanel admin={admin} /></TabPanel>
            <TabPanel px={0} pt={0}><CredentialsPanel admin={admin} /></TabPanel>
            <TabPanel px={0} pt={0}><TrafficPanel admin={admin} /></TabPanel>
            <TabPanel px={0} pt={0}><LinksPanel admin={admin} /></TabPanel>
          </TabPanels>
        </Tabs>
      )}
    </ModalShell>
  );
};
