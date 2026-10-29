import {
  Alert,
  AlertIcon,
  Box,
  Button,
  chakra,
  HStack,
  Icon,
  SimpleGrid,
  Text,
  VStack,
} from "@chakra-ui/react";
import {
  CheckCircleIcon,
  ChevronRightIcon,
  Cog6ToothIcon,
  NoSymbolIcon,
  TrashIcon,
  UserMinusIcon,
} from "@heroicons/react/24/outline";
import { ConfirmModal, InfoTip, Sheet, StatTile } from "components/shared";
import { AdminListItem } from "hooks/useAdmins";
import {
  useActivateAdminUsers,
  useDeleteAdminUsers,
  useDisableAdminUsers,
} from "hooks/useAdminManagement";
import { useAppToast } from "hooks/useAppToast";
import { FC, ReactNode, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { formatBytes, numberWithCommas } from "utils/formatByte";
import { DeleteAdminDialog } from "./DeleteAdminDialog";
import { Trait, TraitBadges } from "./traits";

const muted = { color: "gray.600", _dark: { color: "gray.400" } };

/** A full-width 48px row that does one thing; `chevron` when it opens more. */
const ActionRow: FC<{
  icon: typeof TrashIcon;
  onClick: () => void;
  colorScheme?: string;
  isDisabled?: boolean;
  chevron?: boolean;
  children: ReactNode;
}> = ({ icon, onClick, colorScheme = "gray", isDisabled, chevron, children }) => (
  <Button
    variant="ghost"
    colorScheme={colorScheme}
    w="full"
    minH="48px"
    px={2}
    justifyContent="flex-start"
    fontWeight="medium"
    fontSize="sm"
    leftIcon={<Icon as={icon} boxSize="18px" />}
    rightIcon={chevron ? <Icon as={ChevronRightIcon} boxSize="14px" color="gray.400" /> : undefined}
    isDisabled={isDisabled}
    onClick={onClick}
  >
    {/* Fills the row, so the chevron sits at the far edge. */}
    <chakra.span flex="1" textAlign="left" whiteSpace="normal">
      {children}
    </chakra.span>
  </Button>
);

type BulkAction = "disable" | "activate" | "delete";

type Props = {
  admin: AdminListItem | null;
  traitsOf: (admin: AdminListItem) => Trait[];
  onClose: () => void;
  /** Opens the (still tabbed) settings dialog over this sheet. */
  onEdit: (admin: AdminListItem) => void;
  onDeleted: () => void;
};

/**
 * One admin, opened from its row: the two numbers that matter, its settings,
 * and what can be done to its users. Destructive actions sit last, in red.
 */
export const AdminSheet: FC<Props> = ({ admin, traitsOf, onClose, onEdit, onDeleted }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  // Keep rendering the last admin through the close animation.
  const last = useRef<AdminListItem | null>(null);
  if (admin) last.current = admin;
  const shown = admin ?? last.current;
  const traits = shown ? traitsOf(shown) : [];

  const [bulk, setBulk] = useState<BulkAction | null>(null);
  // Same trick for the bulk dialog: a red "Delete all users" must not turn
  // into "Disable all users", and drop its type-to-confirm gate, mid-exit.
  const lastBulk = useRef<BulkAction | null>(null);
  if (bulk) lastBulk.current = bulk;
  const shownBulk = bulk ?? lastBulk.current;
  const [removing, setRemoving] = useState(false);
  // Closing the sheet (Back included) closes what was opened from it.
  useEffect(() => {
    if (!admin) {
      setBulk(null);
      setRemoving(false);
    }
  }, [admin]);
  const disableUsers = useDisableAdminUsers();
  const activateUsers = useActivateAdminUsers();
  const deleteUsers = useDeleteAdminUsers();

  const runBulk = async () => {
    if (!bulk || !shown) return;
    const username = shown.username;
    try {
      if (bulk === "delete") {
        const res = await deleteUsers.mutateAsync(username);
        toast.success(t("adminUsers.deleted", { count: res?.removed ?? 0, username }));
      } else if (bulk === "disable") {
        await disableUsers.mutateAsync(username);
        toast.success(t("adminUsers.disabled", { username }));
      } else {
        await activateUsers.mutateAsync(username);
        toast.success(t("adminUsers.activated", { username }));
      }
      setBulk(null);
    } catch (e) {
      toast.error(e, t("adminUsers.error"));
    }
  };

  return (
    <>
      <Sheet isOpen={!!admin} onClose={onClose} title={shown?.username}>
        {shown && (
          <VStack align="stretch" spacing={5}>
            {traits.length > 0 && (
              <Box mt={-1}>
                <TraitBadges traits={traits} />
              </Box>
            )}
            <SimpleGrid columns={2} spacing={2}>
              <StatTile
                label={t("admins.columns.users")}
                value={numberWithCommas(shown.users_count ?? 0)}
              />
              <StatTile
                label={t("admins.columns.traffic")}
                value={formatBytes(shown.users_usage ?? 0)}
                caption={t("admins.sheet.total")}
              />
            </SimpleGrid>

            <ActionRow icon={Cog6ToothIcon} chevron onClick={() => onEdit(shown)}>
              {t("admins.sheet.settings")}
            </ActionRow>

            <Box>
              <HStack spacing={1} px={2} pb={1} minH="28px">
                <Text fontSize="xs" fontWeight="semibold" {...muted}>
                  {t("admins.sheet.usersOf", { username: shown.username })}
                </Text>
                {shown.is_sudo && (
                  <InfoTip label={t("admins.sheet.sudoInfo")}>
                    {t("admins.delete.sudoBlocked")}
                  </InfoTip>
                )}
              </HStack>
              <ActionRow icon={NoSymbolIcon} colorScheme="orange" onClick={() => setBulk("disable")}>
                {t("adminUsers.disable")}
              </ActionRow>
              <ActionRow icon={CheckCircleIcon} colorScheme="orange" onClick={() => setBulk("activate")}>
                {t("adminUsers.activate")}
              </ActionRow>
              {/* The backend refuses a sudo admin's users and the sudo itself:
                  disabled here so the refusal shows before the tap. */}
              <ActionRow
                icon={TrashIcon}
                colorScheme="red"
                isDisabled={shown.is_sudo}
                onClick={() => setBulk("delete")}
              >
                {t("adminUsers.delete")}
              </ActionRow>
              <ActionRow
                icon={UserMinusIcon}
                colorScheme="red"
                isDisabled={shown.is_sudo}
                onClick={() => setRemoving(true)}
              >
                {t("admins.delete.action")}
              </ActionRow>
            </Box>
          </VStack>
        )}
      </Sheet>

      <DeleteAdminDialog
        admin={removing ? shown : null}
        onClose={() => setRemoving(false)}
        onDeleted={onDeleted}
      />

      {/* All three bulk actions share one dialog. Only the delete arm is
          gated on typing the username — disable/activate are reversible, and
          gating a reversible action trains people to type past the gate. */}
      <ConfirmModal
        isOpen={!!bulk}
        onClose={() => setBulk(null)}
        onConfirm={runBulk}
        icon={
          shownBulk === "delete" ? <TrashIcon width={20} height={20} />
            : shownBulk === "activate" ? <CheckCircleIcon width={20} height={20} />
              : <NoSymbolIcon width={20} height={20} />
        }
        colorScheme={shownBulk === "delete" ? "red" : "orange"}
        title={shownBulk ? t(`adminUsers.${shownBulk}`) : ""}
        prompt={shownBulk && shown
          ? t(`adminUsers.confirm.${shownBulk}`, { username: shown.username })
          : ""}
        confirmLabel={shownBulk ? t(`adminUsers.${shownBulk}`) : ""}
        typeToConfirm={shownBulk === "delete" ? shown?.username : undefined}
      >
        {/* Every one of these endpoints restarts the local core and each
            connected node, which drops EVERY user on the panel briefly — not
            just this admin's. That is the part an operator does not expect. */}
        <Alert status="warning" borderRadius="md" fontSize="xs">
          <AlertIcon />
          {t("adminUsers.restartWarning")}
        </Alert>
      </ConfirmModal>
    </>
  );
};
