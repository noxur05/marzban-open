import { Alert, AlertIcon, Badge, HStack, Select, Skeleton, Text, VStack } from "@chakra-ui/react";
import { ArrowsRightLeftIcon } from "@heroicons/react/24/outline";
import { ConfirmModal, FormField, Hint } from "components/shared";
import { useDashboard } from "contexts/DashboardContext";
import useAdmins from "hooks/useAdmins";
import { useAppToast } from "hooks/useAppToast";
import { useTransferUser } from "hooks/useTransferUser";
import { FC, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

/**
 * Ceiling a `happ_only` admin is held to when it CREATES a user
 * (HAPP_ONLY_MAX_DEVICE_LIMIT, app/routers/user.py). Mirrored here only to
 * warn when a transfer would hand such an admin a user above it — the backend
 * does not re-check on transfer, by design, since happ_only restricts creation
 * rather than ownership.
 */
const HAPP_ONLY_MAX_DEVICE_LIMIT = 20;

/**
 * Reassign one user to a different owning admin. Sudo only — the trigger (the
 * owner badge in the users table) renders only for sudo, and
 * `PUT /api/user/{u}/set-owner` is sudo-gated regardless.
 *
 * The dialog exists to name consequences that are invisible in the act of
 * picking a new owner. Each warning below corresponds to something the backend
 * does silently.
 */
export const TransferUserModal: FC = () => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const user = useDashboard((s) => s.transferUser);
  const close = () => useDashboard.setState({ transferUser: null });
  const { admins, isLoadingAdmins, isErrorAdmins } = useAdmins(!!user);
  const transfer = useTransferUser();
  const [target, setTarget] = useState("");

  // Reset the choice whenever a different user is opened, so the previous
  // transfer's target is never pre-selected against the wrong user.
  useEffect(() => setTarget(""), [user?.username]);

  const currentOwner = user?.admin?.username ?? null;

  // Only real admins (the SUDOERS env-bootstrap account has no `admins` row
  // and cannot own users), and never the owner the user already has.
  const candidates = useMemo(
    () =>
      admins.filter((a) => typeof a.id === "number" && a.username !== currentOwner),
    [admins, currentOwner]
  );

  const targetAdmin = candidates.find((a) => a.username === target);
  // Happ and Micky users are both device-limited and hold an app link that
  // encodes the owner's domain.
  const isAppBound = user?.client_type === "happ" || user?.client_type === "micky";
  const appName = user?.client_type === "micky" ? "Micky" : "Happ";

  // `happ_only` restricts what an admin may CREATE, not what it may own — a
  // transfer is a sudo action and goes through. Flag the two shapes such an
  // admin could not have produced itself, rather than blocking them.
  const happOnlyNonHapp = !!targetAdmin?.happ_only && !isAppBound;
  const happOnlyOverLimit =
    !!targetAdmin?.happ_only &&
    isAppBound &&
    (user?.device_limit ?? 0) > HAPP_ONLY_MAX_DEVICE_LIMIT;

  const run = async () => {
    if (!user || !target) return;
    try {
      await transfer.mutateAsync({ username: user.username, adminUsername: target });
      toast.success(t("transfer.done", { username: user.username, admin: target }));
      close();
    } catch (e) {
      toast.error(e, t("transfer.error"));
    }
  };

  return (
    <ConfirmModal
      isOpen={!!user}
      onClose={close}
      onConfirm={run}
      icon={<ArrowsRightLeftIcon width={20} height={20} />}
      colorScheme="primary"
      title={t("transfer.title")}
      prompt={
        <HStack spacing={1.5} flexWrap="wrap">
          <Text as="span">{t("transfer.prompt", { username: user?.username ?? "" })}</Text>
          {currentOwner && (
            <Badge fontSize="2xs" textTransform="none">{currentOwner}</Badge>
          )}
        </HStack>
      }
      confirmLabel={t("transfer.action")}
      isConfirmDisabled={!target}
    >
      <VStack align="stretch" spacing={3}>
        <FormField label={t("transfer.newOwner")}>
          {isLoadingAdmins ? (
            <Skeleton h="32px" borderRadius="md" />
          ) : isErrorAdmins ? (
            <Alert status="error" borderRadius="md" fontSize="xs">
              <AlertIcon />
              {t("transfer.adminsError")}
            </Alert>
          ) : candidates.length === 0 ? (
            <Hint>{t("transfer.noCandidates")}</Hint>
          ) : (
            <Select
              size="sm"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder={t("transfer.selectPlaceholder")}
            >
              {candidates.map((a) => (
                <option key={a.username} value={a.username}>
                  {a.username}
                  {a.is_sudo ? " (sudo)" : ""}
                  {a.happ_only ? " · device-limited" : ""}
                </option>
              ))}
            </Select>
          )}
        </FormField>

        <Hint>{t("transfer.usageNote")}</Hint>

        {/* Happ and Micky links encode the OWNER's subscription prefix. When the target
            admin has none set, build_subscription_url falls back to the
            panel default — so naming "the new admin's domain" would be false
            in exactly that case. */}
        {isAppBound && targetAdmin && (
          <Alert status="info" borderRadius="md" fontSize="xs">
            <AlertIcon />
            {targetAdmin.subscription_url
              ? t("transfer.happNoteDomain", { domain: targetAdmin.subscription_url, app: appName })
              : t("transfer.happNoteDefault", { admin: targetAdmin.username, app: appName })}
          </Alert>
        )}

        {happOnlyNonHapp && (
          <Alert status="warning" borderRadius="md" fontSize="xs">
            <AlertIcon />
            {t("transfer.happOnlyMismatch", { admin: target })}
          </Alert>
        )}

        {happOnlyOverLimit && (
          <Alert status="warning" borderRadius="md" fontSize="xs">
            <AlertIcon />
            {t("transfer.happOnlyDeviceLimit", {
              admin: target,
              limit: HAPP_ONLY_MAX_DEVICE_LIMIT,
              deviceLimit: user?.device_limit ?? 0,
            })}
          </Alert>
        )}
      </VStack>
    </ConfirmModal>
  );
};
