import {
  Box,
  chakra,
  HStack,
  Icon,
  IconButton,
  Popover,
  PopoverArrow,
  PopoverBody,
  PopoverContent,
  PopoverTrigger,
  Skeleton,
  Text,
} from "@chakra-ui/react";
import { InformationCircleIcon } from "@heroicons/react/24/outline";
import { FC, ReactNode } from "react";

/**
 * An ⓘ that opens a one-line explanation on tap (a toggletip: a hover tooltip
 * never opens on a touch screen). Explanations live here, never as a
 * paragraph on the page.
 */
export const InfoTip: FC<{ label: string; children: ReactNode }> = ({
  label,
  children,
}) => (
  <Popover placement="bottom-start" isLazy>
    <PopoverTrigger>
      <IconButton
        aria-label={label}
        icon={<Icon as={InformationCircleIcon} boxSize="16px" />}
        size="xs"
        variant="ghost"
        minW="28px"
        h="28px"
        color="gray.500"
      />
    </PopoverTrigger>
    <PopoverContent
      w="auto"
      maxW="min(280px, calc(100vw - 32px))"
      fontSize="sm"
    >
      <PopoverArrow />
      <PopoverBody>{children}</PopoverBody>
    </PopoverContent>
  </Popover>
);

/** "▲ 8%" / "▼ 3%" against the previous period, in ink (not a status colour:
 * more traffic or fewer devices is not good or bad by itself). */
export const Delta: FC<{ pct: number | null; title: string }> = ({
  pct,
  title,
}) => {
  if (pct === null || !Number.isFinite(pct)) return null;
  const rounded =
    Math.abs(pct) >= 10 ? Math.round(pct) : Math.round(pct * 10) / 10;
  const arrow = rounded > 0 ? "▲" : rounded < 0 ? "▼" : "";
  return (
    <Text
      as="span"
      fontSize="xs"
      fontWeight="medium"
      color="gray.700"
      _dark={{ color: "gray.300" }}
      title={title}
      aria-label={`${title}: ${rounded > 0 ? "+" : ""}${rounded}%`}
      whiteSpace="nowrap"
    >
      {arrow} {Math.abs(rounded)}%
    </Text>
  );
};

type StatTileProps = {
  label: ReactNode;
  value: ReactNode;
  /** What the value is ("daily average", "of all devices"); one short label. */
  caption?: ReactNode;
  delta?: ReactNode;
  /** One-line explanation behind an ⓘ (not on a clickable tile: no button in a button). */
  info?: { label: string; text: ReactNode };
  /** Makes the tile a toggle (the Overview's metric switch) or a link to details. */
  onClick?: () => void;
  isSelected?: boolean;
  /** Left edge in an identity colour (Micky). */
  accent?: string;
  isLoading?: boolean;
};

/** One headline number: label, value, a short caption and an optional change. */
export const StatTile: FC<StatTileProps> = ({
  label,
  value,
  caption,
  delta,
  info,
  onClick,
  isSelected,
  accent,
  isLoading,
}) => {
  const interactive = !!onClick;
  return (
    <chakra.div
      as={interactive ? "button" : "div"}
      {...(interactive ? { type: "button" } : {})}
      onClick={onClick}
      aria-pressed={
        interactive && isSelected !== undefined ? isSelected : undefined
      }
      textAlign="left"
      w="full"
      minW={0}
      minH="76px"
      // Three tiles share a 320px phone: tight padding below sm.
      px={{ base: 2, sm: 3, md: 4 }}
      py={{ base: 2.5, md: 3 }}
      borderWidth="1px"
      borderRadius="lg"
      borderColor={isSelected ? "primary.500" : "gray.200"}
      // The theme's primary.50 is a mid blue, so the selected fill is a tint of 500.
      bg={isSelected ? "rgba(57, 111, 228, 0.07)" : "transparent"}
      boxShadow={
        isSelected
          ? "inset 0 0 0 1px var(--chakra-colors-primary-500)"
          : undefined
      }
      borderLeftWidth={accent ? "3px" : "1px"}
      borderLeftColor={accent ?? (isSelected ? "primary.500" : "gray.200")}
      transition="background 0.15s, border-color 0.15s"
      _hover={interactive && !isSelected ? { bg: "gray.50" } : undefined}
      _focusVisible={{
        outline: "2px solid",
        outlineColor: "primary.500",
        outlineOffset: "2px",
      }}
      _dark={{
        borderColor: isSelected ? "primary.300" : "gray.600",
        borderLeftColor: accent ?? (isSelected ? "primary.300" : "gray.600"),
        bg: isSelected ? "rgba(97, 140, 233, 0.14)" : "transparent",
        boxShadow: isSelected
          ? "inset 0 0 0 1px var(--chakra-colors-primary-300)"
          : undefined,
        _hover:
          interactive && !isSelected ? { bg: "whiteAlpha.50" } : undefined,
      }}
    >
      <HStack spacing={1} justify="space-between" minH="20px">
        <Text
          fontSize={{ base: "xs", md: "sm" }}
          color="gray.600"
          _dark={{ color: "gray.400" }}
          noOfLines={1}
        >
          {label}
        </Text>
        {info && (
          <Box mr={-1.5} my={-1}>
            <InfoTip label={info.label}>{info.text}</InfoTip>
          </Box>
        )}
      </HStack>
      {isLoading ? (
        <Skeleton h="26px" mt={1} w="70%" borderRadius="md" />
      ) : (
        <Text
          fontSize={{ base: "md", sm: "lg", md: "2xl" }}
          fontWeight="semibold"
          lineHeight="short"
          mt={0.5}
          noOfLines={1}
          sx={{ fontVariantNumeric: "tabular-nums" }}
        >
          {value}
        </Text>
      )}
      <Text
        fontSize="xs"
        mt={0.5}
        minH="18px"
        color="gray.500"
        _dark={{ color: "gray.400" }}
        // Wraps rather than cuts ("в среднем за день" on a narrow phone).
        noOfLines={2}
      >
        {caption}
      </Text>
      {delta && <Box minH="18px">{!isLoading && delta}</Box>}
    </chakra.div>
  );
};
