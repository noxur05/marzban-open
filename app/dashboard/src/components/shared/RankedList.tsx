import { Box, Button, chakra, Flex, Grid, Icon, Text } from "@chakra-ui/react";
import { ChevronRightIcon } from "@heroicons/react/24/outline";
import { FC, ReactNode } from "react";

export type RankedItem = {
  key: string;
  title: ReactNode;
  /** One secondary fact under the title (e.g. the owning admin), never a run of facts. */
  subtitle?: ReactNode;
  /** Chips under the title; they wrap, so none is ever cut off. */
  badges?: ReactNode;
  /** Identity colour swatch (apps). */
  swatch?: string;
  value: ReactNode;
  /** The value's share or a second figure, right-aligned in its own column. */
  secondary?: ReactNode;
  /** 0..1, the bar's length relative to the list's largest value. */
  bar?: number;
  onClick?: () => void;
  /** Tinted: the row just added. */
  highlight?: boolean;
};

type RankedListProps = {
  items: RankedItem[];
  /** Rows shown on the page; the rest is behind `viewAll`. */
  limit?: number;
  /** Number each row (people, admins); apps use swatches instead. */
  ranked?: boolean;
  barColor?: string;
  /** Width of the value column: wide enough for "831.49 GB" when it holds bytes. */
  valueWidth?: string;
  header?: { title: ReactNode; value: ReactNode; secondary?: ReactNode };
  viewAll?: { label: ReactNode; onClick: () => void };
};

const muted = { color: "gray.600", _dark: { color: "gray.400" } };
const numeric = {
  textAlign: "right",
  sx: { fontVariantNumeric: "tabular-nums" },
} as const;

/**
 * The Statistics page's breakdown list: name, a short proportional bar, the
 * value and its share in fixed right-aligned columns, so rows compare at a
 * glance on a phone. A row with `onClick` opens its details; the page shows
 * the top `limit` rows and "View all" opens the rest in a sheet, so the page
 * never grows with the data.
 */
export const RankedList: FC<RankedListProps> = ({
  items,
  limit,
  ranked,
  barColor = "primary.500",
  valueWidth = "64px",
  header,
  viewAll,
}) => {
  const rows = limit ? items.slice(0, limit) : items;
  const hasBars = items.some((i) => i.bar !== undefined);
  const hasSwatch = items.some((i) => i.swatch);
  const hasSecondary =
    items.some((i) => i.secondary !== undefined) || !!header?.secondary;
  const clickable = items.some((i) => i.onClick);
  // On a phone the bar runs under the name, so the name keeps the width;
  // from md up it is a column of its own.
  const template = (withBar: boolean) =>
    [
      ranked ? "20px" : null,
      hasSwatch ? "10px" : null,
      "minmax(0, 1fr)",
      withBar ? "clamp(40px, 22%, 160px)" : null,
      valueWidth,
      hasSecondary ? "52px" : null,
      clickable ? "16px" : null,
    ]
      .filter(Boolean)
      .join(" ");
  const columns = { base: template(false), md: template(hasBars) };
  const bar = (item: RankedItem, h: string) =>
    item.bar !== undefined && item.bar > 0 ? (
      <Box
        h={h}
        w={`${Math.min(Math.max(item.bar * 100, 2), 100)}%`}
        borderRightRadius="3px"
        bg={barColor}
      />
    ) : null;

  return (
    <Box>
      {header && (
        <Grid
          templateColumns={columns}
          columnGap={2}
          px={1}
          pb={1.5}
          fontSize="xs"
          {...muted}
        >
          {ranked && <span />}
          {hasSwatch && <span />}
          <Text noOfLines={1}>{header.title}</Text>
          {hasBars && <Box display={{ base: "none", md: "block" }} />}
          {/* A long label ("Устройства") may spill left into free space instead of being cut. */}
          <Flex justify="flex-end" minW={0}>
            <Text whiteSpace="nowrap">{header.value}</Text>
          </Flex>
          {hasSecondary && (
            <Flex justify="flex-end" minW={0}>
              <Text whiteSpace="nowrap">{header.secondary}</Text>
            </Flex>
          )}
          {clickable && <span />}
        </Grid>
      )}
      <Box role="list">
        {rows.map((item, i) => (
          // The list item wraps the row, so a clickable row keeps its button role.
          <Box key={item.key} role="listitem" data-key={item.key}>
            <Grid
              as={item.onClick ? "button" : "div"}
              {...(item.onClick
                ? { type: "button", onClick: item.onClick }
                : {})}
              templateColumns={columns}
              columnGap={2}
              alignItems="center"
              w="full"
              minH="48px"
              px={1}
              py={1.5}
              textAlign="left"
              fontSize="sm"
              borderTopWidth="1px"
              borderColor="gray.100"
              borderRadius={0}
              // This theme's primary.50 is a mid blue: far too loud for a row.
              bg={item.highlight ? "blue.50" : undefined}
              _dark={{
                borderColor: "gray.700",
                bg: item.highlight ? "whiteAlpha.100" : undefined,
                _hover: item.onClick ? { bg: item.highlight ? "whiteAlpha.200" : "whiteAlpha.50" } : undefined,
              }}
              _hover={item.onClick ? { bg: item.highlight ? "blue.100" : "gray.50" } : undefined}
              _focusVisible={{
                outline: "2px solid",
                outlineColor: "primary.500",
                outlineOffset: "-2px",
              }}
            >
              {ranked && (
                <Text fontSize="xs" {...muted} {...numeric}>
                  {i + 1}
                </Text>
              )}
              {hasSwatch && (
                <Box
                  w="10px"
                  h="10px"
                  borderRadius="2px"
                  bg={item.swatch ?? "transparent"}
                />
              )}
              <Box minW={0}>
                <Text fontWeight="medium" noOfLines={1}>
                  {item.title}
                </Text>
                {item.subtitle && (
                  <Text fontSize="xs" noOfLines={1} {...muted}>
                    {item.subtitle}
                  </Text>
                )}
                {item.badges && <Box mt={0.5}>{item.badges}</Box>}
                {hasBars && (
                  <Box display={{ base: "block", md: "none" }} mt={1}>
                    {bar(item, "3px")}
                  </Box>
                )}
              </Box>
              {hasBars && (
                <Box h="6px" display={{ base: "none", md: "block" }}>
                  {bar(item, "full")}
                </Box>
              )}
              <Text fontWeight="semibold" whiteSpace="nowrap" {...numeric}>
                {item.value}
              </Text>
              {hasSecondary && (
                <Text fontSize="xs" whiteSpace="nowrap" {...muted} {...numeric}>
                  {item.secondary}
                </Text>
              )}
              {clickable && (
                <chakra.span display="flex" justifyContent="flex-end">
                  {item.onClick && (
                    <Icon
                      as={ChevronRightIcon}
                      boxSize="14px"
                      color="gray.400"
                    />
                  )}
                </chakra.span>
              )}
            </Grid>
          </Box>
        ))}
      </Box>
      {viewAll && limit !== undefined && items.length > limit && (
        <Button
          variant="ghost"
          size="sm"
          w="full"
          mt={1}
          minH="44px"
          justifyContent="space-between"
          rightIcon={<Icon as={ChevronRightIcon} boxSize="14px" />}
          onClick={viewAll.onClick}
          fontWeight="medium"
          colorScheme="primary"
        >
          {viewAll.label}
        </Button>
      )}
    </Box>
  );
};
