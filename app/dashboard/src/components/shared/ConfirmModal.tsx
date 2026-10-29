import { Button, Input, Text, VStack } from "@chakra-ui/react";
import { FC, ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ModalActions, ModalShell } from "./ModalShell";

export type ConfirmModalProps = {
  isOpen: boolean;
  onClose: () => void;
  /** Resolves when the action is done; rejections are left to the caller's
   * mutation error handling (the dialog just stops its spinner). */
  onConfirm: () => Promise<unknown> | void;
  icon: ReactNode;
  /** `red` for destructive, `orange` for irreversible-but-safe, `primary`
   * for a plain "are you sure". Drives both the icon badge and the button. */
  colorScheme?: "red" | "orange" | "primary";
  title: ReactNode;
  /** Body text; pass `<Trans>` for bold fragments like the delete prompt. */
  prompt?: ReactNode;
  confirmLabel?: ReactNode;
  /** When set, the user must type this word before the confirm button
   * enables — for panel-wide, non-undoable actions. */
  typeToConfirm?: string;
  /** Disable the confirm button for a reason the dialog owns — e.g. a required
   * choice in `children` that has not been made yet. Combines with
   * `typeToConfirm`; either one blocks. */
  isConfirmDisabled?: boolean;
  children?: ReactNode;
};

/**
 * The panel's confirmation dialog, generalised from DeleteUserModal /
 * ResetUserUsageModal / RevokeSubscriptionModal: coloured icon badge, bold
 * title, muted prompt, full-width Cancel + action buttons. Replaces every
 * `window.confirm`.
 */
export const ConfirmModal: FC<ConfirmModalProps> = ({
  isOpen,
  onClose,
  onConfirm,
  icon,
  colorScheme = "red",
  title,
  prompt,
  confirmLabel,
  typeToConfirm,
  isConfirmDisabled = false,
  children,
}) => {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState("");
  useEffect(() => {
    if (!isOpen) setTyped("");
  }, [isOpen]);

  const isLoading = busy;
  const gated =
    isConfirmDisabled || (!!typeToConfirm && typed.trim() !== typeToConfirm);

  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      size="sm"
      icon={icon}
      iconColor={colorScheme}
      title={title}
      isBusy={isLoading}
      footer={
        <ModalActions>
          <Button size="sm" variant="outline" onClick={onClose} isDisabled={isLoading}>
            {t("cancel")}
          </Button>
          <Button
            size="sm"
            colorScheme={colorScheme}
            onClick={confirm}
            isLoading={isLoading}
            isDisabled={gated}
          >
            {confirmLabel ?? t("confirm")}
          </Button>
        </ModalActions>
      }
    >
      <VStack align="stretch" spacing={3}>
        {prompt && (
          <Text fontSize="sm" color="gray.600" _dark={{ color: "gray.400" }}>
            {prompt}
          </Text>
        )}
        {children}
        {typeToConfirm && (
          <VStack align="stretch" spacing={1}>
            <Text fontSize="xs" color="gray.600" _dark={{ color: "gray.400" }}>
              {t("shared.typeToConfirm", { word: typeToConfirm })}
            </Text>
            <Input
              size="sm"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={typeToConfirm}
              autoComplete="off"
              isDisabled={isLoading}
            />
          </VStack>
        )}
      </VStack>
    </ModalShell>
  );
};
