import { Box, Button, chakra, Text } from "@chakra-ui/react";
import { ReactComponent as AddFileIcon } from "assets/add_file.svg";
import { FC, ReactNode } from "react";

const Illustration = chakra(AddFileIcon);

export type EmptyStateProps = {
  title: ReactNode;
  description?: ReactNode;
  /** Primary call to action, e.g. "Add host". */
  actionLabel?: ReactNode;
  onAction?: () => void;
  /** Compact variant for inside a modal (smaller illustration, less padding). */
  compact?: boolean;
};

/**
 * The users table's empty section (illustration + muted text + primary CTA),
 * lifted out so the fork's lists show the same thing instead of one grey
 * sentence.
 */
export const EmptyState: FC<EmptyStateProps> = ({
  title,
  description,
  actionLabel,
  onAction,
  compact,
}) => (
  <Box
    padding="5"
    py={compact ? 6 : 8}
    display="flex"
    alignItems="center"
    flexDirection="column"
    gap={3}
    w="full"
  >
    <Illustration
      maxHeight={compact ? "120px" : "200px"}
      maxWidth={compact ? "120px" : "200px"}
      _dark={{
        'path[fill="#fff"]': { fill: "gray.800" },
        'path[fill="#f2f2f2"], path[fill="#e6e6e6"], path[fill="#ccc"]': {
          fill: "gray.700",
        },
        'circle[fill="#3182CE"]': { fill: "primary.300" },
      }}
      _light={{
        'path[fill="#f2f2f2"], path[fill="#e6e6e6"], path[fill="#ccc"]': {
          fill: "gray.300",
        },
        'circle[fill="#3182CE"]': { fill: "primary.500" },
      }}
    />
    <Text fontWeight="medium" color="gray.600" _dark={{ color: "gray.400" }}>
      {title}
    </Text>
    {description && (
      <Text
        fontSize="sm"
        textAlign="center"
        color="gray.600"
        _dark={{ color: "gray.400" }}
        maxW="360px"
      >
        {description}
      </Text>
    )}
    {actionLabel && onAction && (
      <Button size="sm" minH="44px" colorScheme="primary" onClick={onAction}>
        {actionLabel}
      </Button>
    )}
  </Box>
);
