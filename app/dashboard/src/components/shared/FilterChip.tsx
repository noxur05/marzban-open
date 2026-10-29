import {
  Button,
  ButtonGroup,
  Icon,
  IconButton,
  Popover,
  PopoverBody,
  PopoverContent,
  PopoverTrigger,
  Portal,
  Text,
} from "@chakra-ui/react";
import {
  CheckIcon,
  ChevronDownIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import { useIsPhone } from "hooks/useIsPhone";
import { FC, ReactNode } from "react";
import { Sheet } from "./Sheet";

/** A 44px option row with a check on the selected one. */
export const OptionRow: FC<{
  selected: boolean;
  onClick: () => void;
  children: ReactNode;
}> = ({ selected, onClick, children }) => (
  <Button
    variant="ghost"
    justifyContent="space-between"
    w="full"
    minH="44px"
    px={3}
    fontWeight={selected ? "semibold" : "normal"}
    onClick={onClick}
    aria-pressed={selected}
    rightIcon={
      selected ? (
        <Icon as={CheckIcon} boxSize="16px" color="primary.500" />
      ) : undefined
    }
  >
    <Text as="span" noOfLines={1} textAlign="left">
      {children}
    </Text>
  </Button>
);

type ChipProps = {
  icon: typeof CheckIcon;
  label: ReactNode;
  title: string;
  isOpen: boolean;
  onOpen: () => void;
  onClose: () => void;
  children: ReactNode;
  /** Shown attached to the chip when the filter is set, to clear it in one tap. */
  onClear?: () => void;
  /** Cap the label's width (an admin name can be any length); otherwise the row wraps. */
  truncate?: boolean;
  clearLabel?: string;
};

/**
 * A filter chip: tapping it opens its options in a bottom sheet on a phone,
 * a popover under the chip from md up.
 */
export const FilterChip: FC<ChipProps> = ({
  icon,
  label,
  title,
  isOpen,
  onOpen,
  onClose,
  children,
  onClear,
  clearLabel,
  truncate,
}) => {
  const sheet = useIsPhone();
  const chip = (
    <Button
      size="sm"
      h="36px"
      variant="outline"
      borderRadius="full"
      fontWeight="medium"
      leftIcon={<Icon as={icon} boxSize="16px" />}
      rightIcon={<Icon as={ChevronDownIcon} boxSize="14px" />}
      onClick={onOpen}
      maxW={truncate ? { base: "150px", md: "260px" } : undefined}
      borderRightRadius={onClear ? 0 : "full"}
    >
      <Text as="span" noOfLines={1}>
        {label}
      </Text>
    </Button>
  );
  const withClear = onClear ? (
    <ButtonGroup isAttached size="sm" variant="outline">
      {sheet ? chip : <PopoverTrigger>{chip}</PopoverTrigger>}
      <IconButton
        aria-label={clearLabel ?? ""}
        icon={<Icon as={XMarkIcon} boxSize="16px" />}
        h="36px"
        borderRightRadius="full"
        onClick={onClear}
      />
    </ButtonGroup>
  ) : sheet ? (
    chip
  ) : (
    <PopoverTrigger>{chip}</PopoverTrigger>
  );

  if (sheet) {
    return (
      <>
        {withClear}
        <Sheet isOpen={isOpen} onClose={onClose} title={title}>
          {children}
        </Sheet>
      </>
    );
  }
  return (
    <Popover isOpen={isOpen} onClose={onClose} placement="bottom-start" isLazy>
      {withClear}
      <Portal>
        <PopoverContent w="auto" minW="300px" maxW="calc(100vw - 24px)">
          <PopoverBody p={2}>{children}</PopoverBody>
        </PopoverContent>
      </Portal>
    </Popover>
  );
};
