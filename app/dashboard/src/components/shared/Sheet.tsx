import {
  Box,
  Drawer,
  DrawerBody,
  DrawerCloseButton,
  DrawerContent,
  DrawerFooter,
  DrawerHeader,
  DrawerOverlay,
} from "@chakra-ui/react";
import { useIsPhone } from "hooks/useIsPhone";
import { FC, ReactNode, RefObject } from "react";

type SheetProps = {
  isOpen: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  /** Actions pinned under the scrolling body (a form's submit). */
  footer?: ReactNode;
  /** A form: the whole screen on a phone, so the keyboard covers the footer
   * (Enter submits) rather than the fields. */
  full?: boolean;
  /** False for a form: a stray tap beside it must not drop what was typed. */
  closeOnOverlayClick?: boolean;
  initialFocusRef?: RefObject<HTMLElement>;
  /** After the closing animation (and any scroll the browser restores). */
  onCloseComplete?: () => void;
};

/**
 * Where a page puts everything that does not fit its one-screen views —
 * "View all" lists, an item's details, the filters on a phone: a bottom sheet
 * on a phone (thumb reach, the page stays visible above it), a side drawer
 * from md up. Started as the Statistics page's sheet.
 */
export const Sheet: FC<SheetProps> = ({
  isOpen,
  onClose,
  title,
  children,
  footer,
  full,
  closeOnOverlayClick,
  initialFocusRef,
  onCloseComplete,
}) => {
  const bottom = useIsPhone();
  const sheet = bottom && !full;
  return (
    <Drawer
      isOpen={isOpen}
      onClose={onClose}
      placement={bottom ? "bottom" : "right"}
      size="md"
      closeOnOverlayClick={closeOnOverlayClick}
      initialFocusRef={initialFocusRef}
      onCloseComplete={onCloseComplete}
    >
      <DrawerOverlay />
      <DrawerContent
        borderTopRadius={sheet ? "2xl" : 0}
        h={bottom && full ? "100dvh" : undefined}
        maxH={bottom ? (full ? "100dvh" : "85dvh") : undefined}
        pb="env(safe-area-inset-bottom)"
      >
        {sheet && (
          <Box
            mx="auto"
            mt={2}
            w="36px"
            h="4px"
            borderRadius="full"
            bg="gray.300"
            _dark={{ bg: "gray.600" }}
          />
        )}
        <DrawerCloseButton w="44px" h="44px" top={sheet ? 3 : 2} right={2} />
        <DrawerHeader fontSize="lg" pr={14} pb={2}>
          {title}
        </DrawerHeader>
        <DrawerBody pb={footer ? 4 : 6}>{children}</DrawerBody>
        {footer && (
          <DrawerFooter
            borderTopWidth="1px"
            borderColor="gray.100"
            _dark={{ borderColor: "gray.700" }}
            px={4}
            py={3}
          >
            {footer}
          </DrawerFooter>
        )}
      </DrawerContent>
    </Drawer>
  );
};
