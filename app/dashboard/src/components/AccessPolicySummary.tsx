import { Badge, Box, Button, Checkbox, HStack, Text, VStack } from "@chakra-ui/react";
import {
  ArrowPathIcon,
  DevicePhoneMobileIcon,
  GlobeAltIcon,
  ShieldExclamationIcon,
  UserIcon,
} from "@heroicons/react/24/outline";
import { ConfirmModal, FormField } from "components/shared";
import { useAppToast } from "hooks/useAppToast";
import { useRegenerateUserSub } from "hooks/useRegenerateSubs";
import { useResetUserDevices } from "hooks/useUserDevices";
import { FC, useEffect, useState } from "react";
import { UseFormReturn, useWatch } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { User } from "types/User";

/**
 * Edit-mode summary of a user's access policy. The policy itself is fixed
 * after creation (the backend does not allow clearing it), so this shows what
 * it is and offers the actions that ARE available: Reset devices (sudo only —
 * device rows feed statistics and billing), Regenerate the app link, and the
 * one-way Outline grant. Creation uses the dialog's own controls.
 */
export const AccessPolicySummary: FC<{
  form: UseFormReturn<any>;
  editingUser: User | null | undefined;
  disabled: boolean;
  canEnableOutline: boolean;
  canResetDevices: boolean;
}> = ({ form, editingUser: user, disabled, canEnableOutline, canResetDevices }) => {
  const { t } = useTranslation();
  const toast = useAppToast();
  const outlineEnabled = !!useWatch({ control: form.control, name: "outline_enabled" });
  const username = user?.username ?? "";
  // Happ and Micky users are both device-limited and hold an app link;
  // everything below applies to either, only the label differs.
  const isHapp = user?.client_type === "happ";
  const isMicky = user?.client_type === "micky";
  const isAppBound = isHapp || isMicky;
  const appName = isMicky ? "Micky" : "Happ";
  const outlineLocked = !!user?.outline_enabled;
  const regen = useRegenerateUserSub();
  const resetDevices = useResetUserDevices(username);
  const [confirm, setConfirm] = useState<null | "regen" | "reset">(null);
  // Local copy of the bound-device count: after a reset the table refetches,
  // but the open dialog must NOT receive a new editingUser (UserDialog resets
  // its whole form on that and would discard the operator's unsaved edits).
  const [deviceCount, setDeviceCount] = useState(user?.device_count ?? 0);
  useEffect(() => setDeviceCount(user?.device_count ?? 0), [user?.username, user?.device_count]);

  const policyLabel = isHapp
    ? t("userDialog.policy.happ")
    : isMicky
    ? t("userDialog.policy.micky")
    : outlineLocked
    ? t("userDialog.policy.outline")
    : t("userDialog.policy.standard");
  const icon = isAppBound ? (
    <DevicePhoneMobileIcon width={16} height={16} />
  ) : outlineLocked ? (
    <GlobeAltIcon width={16} height={16} />
  ) : (
    <UserIcon width={16} height={16} />
  );

  const doRegen = async () => {
    try {
      await regen.mutateAsync(username);
      toast.success(t("regen.done"));
      setConfirm(null);
    } catch (e) {
      toast.error(e, t("regen.error"));
    }
  };
  const doReset = async () => {
    try {
      await resetDevices.mutateAsync();
      setDeviceCount(0);
      toast.success(t("userDialog.policy.resetDevicesDone"));
      setConfirm(null);
    } catch (e) {
      toast.error(e, t("userDialog.policy.resetDevicesError"));
    }
  };

  return (
    <FormField label={t("userDialog.policy.title")} helper={t("userDialog.policy.lockedNote")}>
      <Box borderWidth="1px" borderRadius="md" p={3} _dark={{ borderColor: "gray.600" }}>
        <VStack align="stretch" spacing={3}>
          <HStack justifyContent="space-between" flexWrap="wrap" gap={2}>
            <HStack spacing={2}>
              <Box color="primary.500" display="inline-flex">{icon}</Box>
              <Text fontSize="sm" fontWeight="medium">{policyLabel}</Text>
              {isAppBound && (
                <Badge colorScheme="primary" variant="subtle">
                  {t("userDialog.policy.devicesBadge", { count: deviceCount, limit: user?.device_limit ?? 0 })}
                </Badge>
              )}
            </HStack>
            {isAppBound && (
              <HStack spacing={2}>
                {canResetDevices && (
                  <Button
                    size="xs"
                    variant="outline"
                    colorScheme="orange"
                    leftIcon={<ShieldExclamationIcon width={14} height={14} />}
                    isDisabled={disabled}
                    onClick={() => setConfirm("reset")}
                  >
                    {t("userDialog.policy.resetDevices")}
                  </Button>
                )}
                <Button
                  size="xs"
                  variant="outline"
                  leftIcon={<ArrowPathIcon width={14} height={14} />}
                  isDisabled={disabled}
                  onClick={() => setConfirm("regen")}
                >
                  {t("regen.user")}
                </Button>
              </HStack>
            )}
          </HStack>

          {!isAppBound && canEnableOutline && (
            <FormField
              helper={outlineLocked ? t("userDialog.outlineAccessLocked") : t("userDialog.outlineAccessHelp")}
              isDisabled={disabled || outlineLocked}
            >
              <Checkbox
                colorScheme="primary"
                isChecked={outlineEnabled}
                isDisabled={disabled || outlineLocked}
                onChange={(e) => form.setValue("outline_enabled", e.target.checked, { shouldDirty: true })}
              >
                <Text fontSize="sm">{t("userDialog.outlineAccess")}</Text>
              </Checkbox>
            </FormField>
          )}
        </VStack>
      </Box>

      <ConfirmModal
        isOpen={confirm === "reset"}
        onClose={() => setConfirm(null)}
        onConfirm={doReset}
        icon={<ShieldExclamationIcon width={20} height={20} />}
        colorScheme="orange"
        title={t("userDialog.policy.resetDevices")}
        prompt={t("userDialog.policy.resetDevicesConfirm", { username })}
        confirmLabel={t("userDialog.policy.resetDevices")}
      />
      <ConfirmModal
        isOpen={confirm === "regen"}
        onClose={() => setConfirm(null)}
        onConfirm={doRegen}
        icon={<ArrowPathIcon width={20} height={20} />}
        colorScheme="primary"
        title={t("regen.user")}
        prompt={t("userDialog.policy.regenConfirm", { username, app: appName })}
        confirmLabel={t("regen.user")}
      />
    </FormField>
  );
};
