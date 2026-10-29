import { Alert, AlertIcon, Skeleton, VStack } from "@chakra-ui/react";
import { UserMinusIcon } from "@heroicons/react/24/outline";
import { ConfirmModal, Hint } from "components/shared";
import { AdminListItem } from "hooks/useAdmins";
import { useAdminUserCount, useDeleteAdmin } from "hooks/useAdminManagement";
import { useAppToast } from "hooks/useAppToast";
import { FC, useRef } from "react";
import { useTranslation } from "react-i18next";

/** Sudo-only, PERMANENT. Refuses (before the backend does) while the admin
 * still owns users. `onDeleted` runs once it is gone. */
export const DeleteAdminDialog: FC<{
  admin: AdminListItem | null;
  onClose: () => void;
  onDeleted?: () => void;
}> = ({ admin, onClose, onDeleted }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const remove = useDeleteAdmin();
  // Keep rendering the last admin through the close animation, as the bulk
  // dialog does, so the text and the type-to-confirm word don't blank out.
  const last = useRef<AdminListItem | null>(null);
  if (admin) last.current = admin;
  const shown = admin ?? last.current;
  // Keyed on `shown` and enabled only while open: the close animation keeps
  // the last answer instead of flipping to the empty state.
  const count = useAdminUserCount(shown?.username ?? null, !!admin);
  const owned = count.data ?? 0;
  // isFetching, not isLoading: on reopen the cached count is stale until the
  // refetch lands.
  const checking = count.isFetching;

  const run = async () => {
    if (!admin) return;
    try {
      await remove.mutateAsync(admin.username);
      toast.success(t("admins.delete.done", { username: admin.username }));
      onClose();
      onDeleted?.();
    } catch (e) {
      toast.error(e, t("admins.delete.error"));
    }
  };

  return (
    <ConfirmModal
      isOpen={!!admin}
      onClose={onClose}
      onConfirm={run}
      icon={<UserMinusIcon width={20} height={20} />}
      colorScheme="red"
      title={t("admins.delete.action")}
      prompt={t("admins.delete.prompt", { username: shown?.username ?? "" })}
      confirmLabel={t("admins.delete.action")}
      // A failed count doesn't block: the backend still refuses with a 409.
      isConfirmDisabled={checking || owned > 0}
      typeToConfirm={checking || owned > 0 ? undefined : shown?.username}
    >
      {checking ? (
        <Skeleton h="16px" borderRadius="md" />
      ) : owned > 0 ? (
        <Alert status="error" borderRadius="md" fontSize="sm">
          <AlertIcon />
          {t("admins.delete.hasUsers", { count: owned, username: shown?.username ?? "" })}
        </Alert>
      ) : (
        <VStack align="stretch" spacing={3}>
          {/* GET /admins/usage lists current admins only (routers/admin.py). */}
          <Hint>{t("admins.delete.usageHint")}</Hint>
          {/* The host_admins rows cascade, and an empty set means "every
              user" (share.py), from the next xray.hosts reload on. */}
          <Alert status="warning" borderRadius="md" fontSize="xs">
            <AlertIcon />
            {t("admins.delete.hostsWarning")}
          </Alert>
        </VStack>
      )}
    </ConfirmModal>
  );
};
