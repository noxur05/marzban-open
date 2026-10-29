import {
  Badge,
  Box,
  Button,
  Divider,
  HStack,
  Select,
  Skeleton,
  Switch,
  Tab,
  TabList,
  TabPanel,
  TabPanels,
  Tabs,
  Text,
  Textarea,
  VStack,
} from "@chakra-ui/react";
import { MegaphoneIcon, PowerIcon } from "@heroicons/react/24/outline";
import {
  ConfirmModal,
  FormField,
  Hint,
  ModalActions,
  ModalShell,
} from "components/shared";
import { useAppToast } from "hooks/useAppToast";
import useGetUser from "hooks/useGetUser";
import { useUpdateAdmin } from "hooks/useAdminManagement";
import {
  Announcement,
  AnnouncementSet,
  useAdminAnnouncementHistory,
  useAnnouncementsOverview,
  useDisableAdminAnnouncement,
  useDisableGlobalAnnouncement,
  useDisableMyAnnouncement,
  useGlobalAnnouncementHistory,
  useMyAnnouncements,
  useSetAdminAnnouncement,
  useSetGlobalAnnouncement,
  useSetMyAnnouncement,
} from "hooks/useAnnouncements";
import { FC, ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { formatServerDateTime } from "utils/dates";

const MAX = 200;

type EditorProps = {
  latest: Announcement | null | undefined;
  history: Announcement[] | undefined;
  isLoading: boolean;
  disabled?: boolean;
  helper?: ReactNode;
  onSave: (body: AnnouncementSet) => Promise<unknown>;
  onDisable: () => Promise<unknown>;
};

/**
 * One announcement editor (self / global / per-admin): labelled textarea
 * with a counter, an "enabled" switch as a field, the live/off state, Save,
 * and "Turn off" behind a confirmation. Below it, the append-only history.
 */
const AnnouncementEditor: FC<EditorProps> = ({
  latest,
  history,
  isLoading,
  disabled,
  helper,
  onSave,
  onDisable,
}) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const [text, setText] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirmOff, setConfirmOff] = useState(false);

  useEffect(() => {
    setText(latest?.text ?? "");
    setEnabled(latest?.enabled ?? true);
  }, [latest?.id]);

  const showing = !!latest && latest.enabled;
  const dirty = text.trim() !== (latest?.text ?? "") || enabled !== (latest?.enabled ?? true);

  const save = async () => {
    setSaving(true);
    try {
      await onSave({ text: text.trim(), enabled });
      toast.success(t("announcements.saved"));
    } catch (e) {
      toast.error(e, t("announcements.error"));
    } finally {
      setSaving(false);
    }
  };

  const turnOff = async () => {
    try {
      await onDisable();
      toast.success(t("announcements.disabled"));
      setConfirmOff(false);
    } catch (e) {
      toast.error(e, t("announcements.error"));
    }
  };

  if (isLoading) {
    return (
      <VStack align="stretch" spacing={3}>
        <Skeleton h="88px" borderRadius="md" />
        <Skeleton h="20px" w="60%" />
      </VStack>
    );
  }

  return (
    <VStack align="stretch" spacing={4}>
      <FormField
        label={t("announcements.message")}
        labelAddon={
          <Hint color={text.length >= MAX ? "orange.500" : undefined}>
            {text.length}/{MAX}
          </Hint>
        }
        helper={helper}
        isDisabled={disabled}
      >
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value.slice(0, MAX))}
          placeholder={t("announcements.placeholder")}
          rows={3}
          resize="none"
        />
      </FormField>

      <FormField
        label={t("announcements.enabled")}
        helper={t("announcements.enabledHelp")}
        labelAddon={
          <Switch
            size="sm"
            colorScheme="primary"
            isChecked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            isDisabled={disabled}
            aria-label={t("announcements.enabled")}
          />
        }
        isDisabled={disabled}
      />

      <HStack justifyContent="space-between" alignItems="center">
        <HStack spacing={2}>
          <Hint size="sm">{t("announcements.status")}</Hint>
          {showing ? (
            <Badge colorScheme="green">{t("announcements.live")}</Badge>
          ) : latest ? (
            <Badge>{t("announcements.off")}</Badge>
          ) : (
            <Badge colorScheme="gray">{t("announcements.none")}</Badge>
          )}
        </HStack>
        <ModalActions align="end">
          <Button
            size="sm"
            variant="outline"
            colorScheme="orange"
            leftIcon={<PowerIcon width={14} height={14} />}
            onClick={() => setConfirmOff(true)}
            isDisabled={disabled || !showing}
          >
            {t("announcements.disable")}
          </Button>
          <Button
            size="sm"
            colorScheme="primary"
            onClick={save}
            isLoading={saving}
            isDisabled={disabled || !text.trim() || !dirty}
          >
            {t("announcements.save")}
          </Button>
        </ModalActions>
      </HStack>

      <ConfirmModal
        isOpen={confirmOff}
        onClose={() => setConfirmOff(false)}
        onConfirm={turnOff}
        icon={<PowerIcon width={20} height={20} />}
        colorScheme="orange"
        title={t("announcements.disable")}
        prompt={t("announcements.disableConfirm")}
        confirmLabel={t("announcements.disable")}
      />

      <Divider />
      <History items={history} />
    </VStack>
  );
};

const History: FC<{ items: Announcement[] | undefined }> = ({ items }) => {
  const { t } = useTranslation();
  const rows = items ?? [];
  return (
    <VStack align="stretch" spacing={2}>
      <HStack justifyContent="space-between">
        <Text fontSize="sm" fontWeight="medium">
          {t("announcements.history")}
        </Text>
        <Hint>{t("announcements.appendOnlyNote")}</Hint>
      </HStack>
      {rows.length === 0 ? (
        <Hint>{t("announcements.historyEmpty")}</Hint>
      ) : (
        <VStack align="stretch" spacing={1} maxH="200px" overflowY="auto">
          {rows.map((row) => (
            <HStack
              key={row.id}
              spacing={3}
              px={2}
              py={1.5}
              borderRadius="md"
              alignItems="flex-start"
              _hover={{ bg: "gray.50", _dark: { bg: "gray.750" } }}
            >
              <Hint whiteSpace="nowrap" pt="1px">
                {formatServerDateTime(row.created_at)}
              </Hint>
              <Badge
                colorScheme={row.enabled ? "green" : "gray"}
                variant="subtle"
                flexShrink={0}
                mt="1px"
              >
                {row.enabled ? t("announcements.live") : t("announcements.off")}
              </Badge>
              <Text fontSize="sm" noOfLines={2} title={row.text}>
                {row.text}
              </Text>
            </HStack>
          ))}
        </VStack>
      )}
    </VStack>
  );
};

type Props = { isOpen: boolean; onClose: () => void };

export const AnnouncementsModal: FC<Props> = ({ isOpen, onClose }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const { userData } = useGetUser();
  const isSudo = !!userData.is_sudo;
  const canAnnounce = userData.can_announce !== false;

  const mine = useMyAnnouncements(isOpen);
  const setMine = useSetMyAnnouncement();
  const offMine = useDisableMyAnnouncement();

  const overview = useAnnouncementsOverview(isOpen && isSudo);
  const globalHistory = useGlobalAnnouncementHistory(isOpen && isSudo);
  const setGlobal = useSetGlobalAnnouncement();
  const offGlobal = useDisableGlobalAnnouncement();

  const [selectedAdmin, setSelectedAdmin] = useState("");
  const adminHistory = useAdminAnnouncementHistory(
    isOpen && isSudo && selectedAdmin ? selectedAdmin : null
  );
  const setAdmin = useSetAdminAnnouncement();
  const offAdmin = useDisableAdminAnnouncement();
  const updateAdmin = useUpdateAdmin();

  const adminItems = (overview.data ?? []).filter((i) => i.admin_id !== null);
  const globalItem = overview.data?.find((i) => i.admin_id === null);
  const selectedItem = adminItems.find((i) => i.username === selectedAdmin);

  const busy =
    setMine.isLoading || offMine.isLoading || setGlobal.isLoading ||
    offGlobal.isLoading || setAdmin.isLoading || offAdmin.isLoading;

  const self = (
    <VStack align="stretch" spacing={3}>
      {!canAnnounce && (
        <Box
          fontSize="sm"
          color="orange.600"
          _dark={{ color: "orange.300", borderColor: "orange.500" }}
          borderWidth="1px"
          borderColor="orange.300"
          borderRadius="md"
          p={3}
        >
          {t("announcements.noPermission")}
        </Box>
      )}
      <AnnouncementEditor
        latest={mine.data?.[0] ?? null}
        history={mine.data}
        isLoading={mine.isLoading}
        disabled={!canAnnounce}
        helper={t("announcements.selfHelp")}
        onSave={(body) => setMine.mutateAsync(body)}
        onDisable={() => offMine.mutateAsync()}
      />
    </VStack>
  );

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      size="lg"
      scrollBehavior="inside"
      icon={<MegaphoneIcon width={20} height={20} />}
      title={t("announcements.title")}
      subtitle={t("announcements.subtitle")}
      isBusy={busy}
    >
      {!isSudo ? (
        self
      ) : (
        <Tabs variant="soft-rounded" colorScheme="primary" size="sm" isLazy>
          <TabList mb={4}>
            <Tab>{t("announcements.tab.mine")}</Tab>
            <Tab>{t("announcements.tab.global")}</Tab>
            <Tab>{t("announcements.tab.admins")}</Tab>
          </TabList>
          <TabPanels>
            <TabPanel px={0} pt={0}>
              {self}
            </TabPanel>

            <TabPanel px={0} pt={0}>
              <AnnouncementEditor
                latest={globalItem?.latest}
                history={globalHistory.data}
                isLoading={overview.isLoading || globalHistory.isLoading}
                helper={t("announcements.globalHelp")}
                onSave={(body) => setGlobal.mutateAsync(body)}
                onDisable={() => offGlobal.mutateAsync()}
              />
            </TabPanel>

            <TabPanel px={0} pt={0}>
              <VStack align="stretch" spacing={4}>
                <FormField
                  label={t("announcements.selectAdmin")}
                  helper={t("announcements.perAdminHelp")}
                >
                  <Select
                    value={selectedAdmin}
                    onChange={(e) => setSelectedAdmin(e.target.value)}
                    placeholder={t("announcements.selectAdminPlaceholder")}
                    isDisabled={overview.isLoading}
                  >
                    {adminItems.map((item) => (
                      <option key={item.admin_id ?? ""} value={item.username ?? ""}>
                        {item.username}
                        {item.latest?.enabled ? ` — ${t("announcements.live")}` : ""}
                      </option>
                    ))}
                  </Select>
                </FormField>

                {selectedItem && (
                  <>
                    <FormField
                      label={t("announcements.canAnnounce")}
                      helper={t("announcements.canAnnounceHelp")}
                      labelAddon={
                        <Switch
                          size="sm"
                          colorScheme="primary"
                          isChecked={selectedItem.can_announce !== false}
                          isDisabled={updateAdmin.isLoading}
                          aria-label={t("announcements.canAnnounce")}
                          onChange={(e) =>
                            updateAdmin.mutate(
                              {
                                username: selectedItem.username ?? "",
                                body: { can_announce: e.target.checked },
                              },
                              {
                                onError: (err) => toast.error(err, t("announcements.error")),
                              }
                            )
                          }
                        />
                      }
                    />
                    <AnnouncementEditor
                      key={selectedItem.username ?? ""}
                      latest={selectedItem.latest}
                      history={adminHistory.data}
                      isLoading={adminHistory.isLoading}
                      onSave={(body) =>
                        setAdmin.mutateAsync({ username: selectedItem.username ?? "", body })
                      }
                      onDisable={() => offAdmin.mutateAsync(selectedItem.username ?? "")}
                    />
                  </>
                )}
              </VStack>
            </TabPanel>
          </TabPanels>
        </Tabs>
      )}
    </ModalShell>
  );
};
