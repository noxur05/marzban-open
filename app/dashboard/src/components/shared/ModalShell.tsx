import {
  Modal,
  ModalBody,
  ModalCloseButton,
  ModalContent,
  ModalFooter,
  ModalHeader,
  ModalOverlay,
  ModalProps,
  HStack,
  Text,
  VStack,
} from "@chakra-ui/react";
import { Icon } from "components/Icon";
import { FC, PropsWithChildren, ReactNode } from "react";

export type ModalShellProps = PropsWithChildren<{
  isOpen: boolean;
  onClose: () => void;
  /** Header icon, rendered inside the panel's coloured `Icon` badge. */
  icon?: ReactNode;
  /** Colour scheme of the icon badge (`primary`, `red`, `orange`, ...). */
  iconColor?: string;
  title: ReactNode;
  subtitle?: ReactNode;
  /** Footer content; usually a `ModalActions`. */
  footer?: ReactNode;
  size?: ModalProps["size"];
  scrollBehavior?: ModalProps["scrollBehavior"];
  /** Disables the close button while a mutation is in flight. */
  isBusy?: boolean;
  /** Passed straight to Chakra's Modal (e.g. `closeOnOverlayClick`). */
  modalProps?: Partial<ModalProps>;
}>;

/**
 * The panel's modal chrome, extracted from the existing dialogs
 * (UserDialog, DeleteUserModal, NodesModal): centered, blurred overlay,
 * `mx="3"` so it never touches the screen edge on phones, icon badge + title
 * in the header, close button aligned with it.
 *
 * Every fork-authored dialog renders through this so they look like the
 * ones upstream shipped.
 */
export const ModalShell: FC<ModalShellProps> = ({
  isOpen,
  onClose,
  icon,
  iconColor = "primary",
  title,
  subtitle,
  footer,
  size = "lg",
  scrollBehavior,
  isBusy,
  modalProps,
  children,
}) => (
  <Modal
    isCentered
    isOpen={isOpen}
    onClose={onClose}
    size={size}
    scrollBehavior={scrollBehavior}
    closeOnOverlayClick={!isBusy}
    {...modalProps}
  >
    <ModalOverlay bg="blackAlpha.300" backdropFilter="blur(10px)" />
    <ModalContent mx="3">
      <ModalHeader pt={6}>
        <HStack gap={2} alignItems="center">
          {icon && (
            <Icon color={iconColor}>
              <Text as="span" color="white" display="inline-flex">
                {icon}
              </Text>
            </Icon>
          )}
          <VStack alignItems="flex-start" spacing={0}>
            <Text fontWeight="semibold" fontSize="lg" lineHeight="short">
              {title}
            </Text>
            {subtitle && (
              <Text
                fontSize="sm"
                fontWeight="normal"
                color="gray.600"
                _dark={{ color: "gray.400" }}
              >
                {subtitle}
              </Text>
            )}
          </VStack>
        </HStack>
      </ModalHeader>
      <ModalCloseButton mt={3} isDisabled={isBusy} />
      <ModalBody>{children}</ModalBody>
      {footer && <ModalFooter display="flex">{footer}</ModalFooter>}
    </ModalContent>
  </Modal>
);

/** Footer button row in the panel's style: full-width, `size="sm"`. */
export const ModalActions: FC<PropsWithChildren<{ align?: "stretch" | "end" }>> = ({
  children,
  align = "stretch",
}) => (
  <HStack
    w="full"
    spacing={3}
    justifyContent={align === "end" ? "flex-end" : undefined}
    sx={
      align === "stretch"
        ? { "& > button": { flex: 1 } }
        : undefined
    }
  >
    {children}
  </HStack>
);
